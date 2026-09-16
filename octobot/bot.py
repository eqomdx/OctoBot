from __future__ import annotations

import logging
from datetime import timedelta
from typing import Literal

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from octotracker.announcements import AnnouncementClient
from octotracker.auth import AuthProbe
from octotracker.config import Config
from octotracker.database import Database as TrackerDatabase
from octotracker.embeds import (
    announcement_embeds,
    authcheck_embed,
    help_embed,
    next_shows_embeds,
    incidents_embed,
    radio_status_embed,
    reports_details_embed,
    reports_embed,
    uptime_embed,
)
from octotracker.monitor import Monitor
from octotracker.permissions import (
    MANAGED_COMMANDS,
    command_access_allowed,
    command_role_label,
)
from octotracker.radio import RadioClient
from octotracker.radio_monitor import RadioMonitor
from octotracker.reports import MAX_REPORT_DETAILS
from octotracker.status import StatusClient


LOGGER = logging.getLogger("octobot")
RealmChoice = Literal["C'Thun", "N'Zoth", "Y'Shaarj", "All Realms"]
IssueChoice = Literal["Can't connect", "Disconnecting", "High latency", "Other"]
CommandChoice = Literal[
    "status",
    "uptime",
    "incidents",
    "announcement",
    "report",
    "reports",
    "radio",
]

HELP_DESCRIPTIONS: dict[str, str] = {
    "status": "Check current realm, login, authentication and report status.",
    "uptime": "View OctoWoW uptime statistics.",
    "incidents": "View recent outages and degraded incidents.",
    "announcement": "Read the latest official OctoWoW announcement.",
    "report": "Report a current connection problem.",
    "reports": "View recent community connection reports.",
    "radio": "View Booty Bay Pirate Radio and upcoming shows.",
    "nextshows": "List every upcoming Booty Bay Pirate Radio show with times.",
}

MODERATION_HELP_DESCRIPTIONS: dict[str, str] = {
    "timeout": "Time out a member for an exact duration.",
    "untimeout": "Remove a member's active timeout.",
    "ban": "Ban a user from the server with a recorded reason.",
    "whisper": "Send a user a direct message from the bot.",
    "warn": "Record a warning and DM the member.",
    "note": "Add a staff-only note to a member's /check history.",
    "warnings": "View a member's recorded warnings.",
    "check": "View a member's complete warning/timeout history and reasons.",
    "clearcheck": "Clear a member's recorded /check history.",
    "history": "Show a member's recent server messages.",
    "settings": "Configure OctoBot moderation permissions and behaviour.",
    "word": "Manage the banned-word filter (list / add / remove).",
    "rolesweep": "One-off: strip the legacy role from members holding a superseding role.",
}


class OctoBot(commands.Bot):
    def __init__(self, config: Config):
        self.config = config
        self.guild = discord.Object(id=config.guild_id)
        self.database = TrackerDatabase(config.database_path)
        self.tracker_database = self.database
        from octocop.database import Database as ModerationDatabase
        from octocop.permissions import PermissionService
        self.moderation_database = ModerationDatabase(config.database_path)
        self.moderation_permissions = PermissionService(self.moderation_database)
        self.session: aiohttp.ClientSession | None = None
        self.monitor: Monitor | None = None
        self.radio_client: RadioClient | None = None
        self.radio_monitor: RadioMonitor | None = None

        intents = discord.Intents.default()
        # Required for /history to index message text from guild message events.
        # Enable Message Content Intent in the Discord Developer Portal as well.
        intents.message_content = True
        # Required for role cleanup (member role updates and listing members).
        # Enable Server Members Intent in the Discord Developer Portal as well.
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

        command_definitions = (
            ("status", "Check the current OctoWoW server status.", self.status_command),
            ("uptime", "Show official uptime and recent percentages.", self.uptime_command),
            ("incidents", "Show recent confirmed OctoWoW incidents.", self.incidents_command),
            ("announcement", "Show the latest official announcement.", self.announcement_command),
            ("report", "Report a current OctoWoW connection issue.", self.report_command),
            ("reports", "Show grouped current community reports.", self.reports_command),
            ("reports-details", "Show detailed current reports for moderators.", self.reports_details_command),
            ("reports-clear", "Clear current community reports.", self.reports_clear_command),
            ("radio", "Show Booty Bay Pirate Radio status and upcoming shows.", self.radio_command),
            ("nextshows", "List all upcoming Booty Bay Pirate Radio shows.", self.nextshows_command),
            ("help", "Explain the OctoBot commands you can use.", self.help_slash_command),
            (
                "authcheck",
                "Run an immediate authentication endpoint diagnostic.",
                self.authcheck_command,
            ),
        )
        for name, description, callback in command_definitions:
            self.tree.command(name=name, description=description, guild=self.guild)(callback)

        config_group = app_commands.Group(
            name="config",
            description="Configure OctoBot tracker command access.",
            default_permissions=discord.Permissions(administrator=True),
        )
        command_role_group = app_commands.Group(
            name="command-role",
            description="Configure per-command role access.",
            parent=config_group,
        )
        command_role_group.command(
            name="add", description="Allow a role to use a command."
        )(self.config_role_add_command)
        command_role_group.command(
            name="remove", description="Remove a command's allowed role."
        )(self.config_role_remove_command)
        command_role_group.command(
            name="list", description="List per-command allowed roles."
        )(self.config_role_list_command)
        self.tree.add_command(config_group, guild=self.guild)

    async def setup_hook(self) -> None:
        await self.database.connect()
        await self.moderation_database.connect()
        await self._seed_moderation_defaults()

        from octocop.cogs.moderation import ModerationCog
        from octocop.cogs.roles import RoleCleanupCog
        from octocop.cogs.settings import SettingsCog
        from octocop.cogs.words import WordFilterCog

        # Register moderation commands directly to the configured guild.
        await self.add_cog(ModerationCog(self), guild=self.guild)
        await self.add_cog(SettingsCog(self), guild=self.guild)
        await self.add_cog(RoleCleanupCog(self), guild=self.guild)
        await self.add_cog(WordFilterCog(self), guild=self.guild)
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.request_timeout_seconds),
            headers={"User-Agent": "OctoBot/1.0 (+OctoWoW Discord bot)"},
        )
        self.monitor = Monitor(
            bot=self,
            config=self.config,
            database=self.database,
            status_client=StatusClient(self.session),
            auth_probe=AuthProbe(
                self.config.realmlist_host,
                self.config.auth_port,
                self.config.auth_probe_timeout_seconds,
            ),
            announcement_client=AnnouncementClient(self.session),
        )
        self.radio_client = RadioClient(
            self.session,
            self.config.radio_base_url,
            self.config.radio_station_shortcode,
            schedule_days=self.config.radio_schedule_days,
        )
        self.radio_monitor = RadioMonitor(
            bot=self,
            config=self.config,
            database=self.database,
            client=self.radio_client,
        )

        # Remove the old global development commands and keep exactly one guild copy.
        self.tree.clear_commands(guild=None)
        await self.tree.sync()
        synced = await self.tree.sync(guild=self.guild)
        LOGGER.info(
            "Synced %s guild command(s) to server %s",
            len(synced),
            self.config.guild_id,
        )
        self.monitor.start()
        self.radio_monitor.start()

    async def _seed_moderation_defaults(self) -> None:
        """Seed OctoCop role/settings defaults without overwriting later /settings changes."""
        from octocop.database import RolePermissions
        from octocop.duration import MAX_TIMEOUT_SECONDS

        guild_id = self.config.guild_id
        settings = await self.moderation_database.get_guild_settings(guild_id)
        if not settings.get("mod_log_channel_id") and self.config.log_channel_id is not None:
            await self.moderation_database.set_log_channel(guild_id, self.config.log_channel_id)
            LOGGER.info("Seeded OctoBot moderation log channel: %s", self.config.log_channel_id)

        profiles = []
        if self.config.helper_role_id is not None:
            profiles.append(RolePermissions(
                guild_id=guild_id, role_id=self.config.helper_role_id,
                can_timeout=True, can_untimeout=True, max_timeout_seconds=3600,
            ))
        if self.config.moderator_role_id is not None:
            profiles.append(RolePermissions(
                guild_id=guild_id, role_id=self.config.moderator_role_id,
                can_timeout=True, can_warn=True, can_view_warnings=True,
                can_check=True, can_untimeout=True, can_ban=True, can_whisper=True,
                can_manage_settings=True,
                max_timeout_seconds=MAX_TIMEOUT_SECONDS,
            ))
        if self.config.admin_role_id is not None:
            profiles.append(RolePermissions(
                guild_id=guild_id, role_id=self.config.admin_role_id,
                can_timeout=True, can_warn=True, can_view_warnings=True,
                can_check=True, can_untimeout=True, can_ban=True, can_whisper=True,
                can_manage_settings=True,
                max_timeout_seconds=MAX_TIMEOUT_SECONDS,
            ))

        for profile in profiles:
            existing = await self.moderation_database.get_role_profile(guild_id, profile.role_id)
            if existing is None:
                await self.moderation_database.upsert_role_profile(profile)
                LOGGER.info("Seeded OctoBot moderation role profile: %s", profile.role_id)
            elif (
                self.config.helper_role_id is not None
                and profile.role_id == self.config.helper_role_id
                and existing.can_timeout
                and not existing.can_warn
                and not existing.can_view_warnings
                and not existing.can_check
                and not existing.can_untimeout
                and not existing.can_manage_settings
                and existing.max_timeout_seconds == 3600
            ):
                # Upgrade the original Helper seed from /timeout-only to the intended
                # /timeout + /untimeout profile without touching customized profiles.
                existing.can_untimeout = True
                await self.moderation_database.upsert_role_profile(existing)
                LOGGER.info("Upgraded Helper default profile to allow /untimeout: %s", profile.role_id)

    async def on_ready(self) -> None:
        if self.user is None:
            return
        LOGGER.info("OctoBot logged in as %s (ID: %s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="OctoWoW servers & moderation"
            )
        )

    async def close(self) -> None:
        if self.radio_monitor is not None:
            await self.radio_monitor.stop()
        if self.monitor is not None:
            await self.monitor.stop()
        if self.session is not None and not self.session.closed:
            await self.session.close()
        await self.moderation_database.close()
        await self.database.close()
        await super().close()

    async def status_command(self, interaction: discord.Interaction) -> None:
        if not await self._require_command_access(interaction, "status"):
            return
        await interaction.response.defer(thinking=True)
        try:
            snapshot = await self._monitor().check_status()
            await interaction.followup.send(
                embed=await self._monitor().make_status_embed(snapshot)
            )
        except Exception:
            LOGGER.exception("The /status command failed")
            await interaction.followup.send(
                "OctoBot could not complete the status check. Please try again.",
                ephemeral=True,
            )

    async def uptime_command(self, interaction: discord.Interaction) -> None:
        if not await self._require_command_access(interaction, "uptime"):
            return
        await interaction.response.defer(thinking=True)
        try:
            snapshot = await self._monitor().check_status()
            stats = [
                ("Last 24 hours", await self.database.uptime_stats(timedelta(days=1))),
                ("Last 7 days", await self.database.uptime_stats(timedelta(days=7))),
                ("Last 30 days", await self.database.uptime_stats(timedelta(days=30))),
            ]
            await interaction.followup.send(embed=uptime_embed(snapshot.official, stats))
        except Exception:
            LOGGER.exception("The /uptime command failed")
            await interaction.followup.send(
                "OctoBot could not calculate uptime right now.", ephemeral=True
            )

    async def incidents_command(self, interaction: discord.Interaction) -> None:
        if not await self._require_command_access(interaction, "incidents"):
            return
        await interaction.response.defer(thinking=True)
        try:
            incidents = await self.database.recent_incidents()
            auth_incidents = await self.database.recent_auth_incidents()
            report_incidents = await self.database.recent_report_incidents(
                self._guild_id(interaction)
            )
            await interaction.followup.send(
                embed=incidents_embed(incidents, auth_incidents, report_incidents)
            )
        except Exception:
            LOGGER.exception("The /incidents command failed")
            await interaction.followup.send(
                "OctoBot could not load incident history right now.",
                ephemeral=True,
            )

    async def announcement_command(self, interaction: discord.Interaction) -> None:
        if not await self._require_command_access(interaction, "announcement"):
            return
        await interaction.response.defer(thinking=True)
        try:
            announcement = await self._monitor().latest_announcement()
            if announcement is None:
                await interaction.followup.send(
                    "No official announcements are available yet.", ephemeral=True
                )
                return
            for embed in announcement_embeds(announcement):
                await interaction.followup.send(embed=embed)
        except Exception:
            LOGGER.exception("The /announcement command failed")
            await interaction.followup.send(
                "OctoBot could not load the latest announcement right now.",
                ephemeral=True,
            )

    async def report_command(
        self,
        interaction: discord.Interaction,
        realm: RealmChoice,
        issue: IssueChoice,
        details: app_commands.Range[str, 1, MAX_REPORT_DETAILS] | None = None,
    ) -> None:
        if not await self._require_command_access(interaction, "report"):
            return
        try:
            _, refreshed = await self.database.submit_report(
                self._guild_id(interaction),
                interaction.user.id,
                realm,
                issue,
                details,
                self.config.report_active_minutes,
            )
            verb = "refreshed" if refreshed else "recorded"
            await interaction.response.send_message(
                f"Your **{realm} — {issue}** report was {verb} for "
                f"{self.config.report_active_minutes} minutes.",
                ephemeral=True,
            )
            await self._refresh_dashboard_after_report_change()
        except ValueError as exc:
            if interaction.response.is_done():
                await interaction.followup.send(str(exc), ephemeral=True)
            else:
                await interaction.response.send_message(str(exc), ephemeral=True)
        except Exception:
            LOGGER.exception("The /report command failed")
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Your report was saved, but OctoBot could not refresh the dashboard right now.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "OctoBot could not save your report right now.", ephemeral=True
                )

    async def reports_command(self, interaction: discord.Interaction) -> None:
        if not await self._require_command_access(interaction, "reports"):
            return
        await interaction.response.defer(thinking=True)
        try:
            summary = await self.database.report_summary(
                self._guild_id(interaction),
                self.config.report_active_minutes,
                self.config.report_degraded_threshold,
            )
            await interaction.followup.send(embed=reports_embed(summary))
        except Exception:
            LOGGER.exception("The /reports command failed")
            await interaction.followup.send(
                "OctoBot could not load community reports right now.",
                ephemeral=True,
            )

    async def reports_details_command(self, interaction: discord.Interaction) -> None:
        if not await self._require_moderator(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            reports = await self.database.active_reports(
                self._guild_id(interaction), self.config.report_active_minutes
            )
            await interaction.followup.send(
                embed=reports_details_embed(reports, self.config.report_active_minutes),
                ephemeral=True,
            )
        except Exception:
            LOGGER.exception("The /reports-details command failed")
            await interaction.followup.send(
                "OctoBot could not load report details right now.", ephemeral=True
            )

    async def reports_clear_command(self, interaction: discord.Interaction) -> None:
        if not await self._require_moderator(interaction):
            return
        try:
            count = await self.database.clear_reports(
                self._guild_id(interaction),
                interaction.user.id,
                self.config.report_active_minutes,
            )
            await interaction.response.send_message(
                f"Cleared {count} current community report(s).", ephemeral=True
            )
            await self._refresh_dashboard_after_report_change()
        except Exception:
            LOGGER.exception("The /reports-clear command failed")
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Reports were cleared, but the dashboard could not be refreshed right now.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "OctoBot could not clear reports right now.", ephemeral=True
                )

    async def radio_command(self, interaction: discord.Interaction) -> None:
        if not await self._require_command_access(interaction, "radio"):
            return
        if not self.config.radio_enabled:
            await interaction.response.send_message(
                "Booty Bay Pirate Radio tracking is currently disabled.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        try:
            snapshot = await self._radio_monitor().fetch_current()
            await interaction.followup.send(embed=radio_status_embed(snapshot))
        except Exception:
            LOGGER.exception("The /radio command failed")
            await interaction.followup.send(
                "OctoBot could not read Booty Bay Pirate Radio right now.",
                ephemeral=True,
            )

    async def nextshows_command(self, interaction: discord.Interaction) -> None:
        # Same access rule as /radio: one "radio" entry in /config command-role covers both.
        if not await self._require_command_access(interaction, "radio"):
            return
        if not self.config.radio_enabled:
            await interaction.response.send_message(
                "Booty Bay Pirate Radio tracking is currently disabled.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        try:
            monitor = self._radio_monitor()
            snapshot = await monitor.fetch_current()
            embeds = next_shows_embeds(snapshot, monitor.dj_streams)
            await interaction.followup.send(embed=embeds[0])
            for embed in embeds[1:]:
                await interaction.followup.send(embed=embed)
        except Exception:
            LOGGER.exception("The /nextshows command failed")
            await interaction.followup.send(
                "OctoBot could not read the Booty Bay Pirate Radio schedule right now.",
                ephemeral=True,
            )

    async def help_slash_command(self, interaction: discord.Interaction) -> None:
        command_channel = "any channel where OctoBot can respond"
        visible: list[tuple[str, str]] = []
        for command_name in MANAGED_COMMANDS:
            if await self._has_command_access(interaction, command_name):
                visible.append((command_name, HELP_DESCRIPTIONS[command_name]))
                if command_name == "radio":
                    visible.append(("nextshows", HELP_DESCRIPTIONS["nextshows"]))

        if self._is_moderator(interaction):
            visible.append(("reports-details", "View individual active report details."))
            visible.append(("reports-clear", "Clear active community reports."))
        if self._is_administrator(interaction):
            visible.append(("authcheck", "Run a one-off authentication diagnostic."))
            visible.append(("config command-role", "Configure which roles can use tracker commands."))

        if isinstance(interaction.user, discord.Member):
            mod_perms = await self.moderation_permissions.for_member(interaction.user)
            if mod_perms.can_timeout:
                visible.append(("timeout", MODERATION_HELP_DESCRIPTIONS["timeout"]))
            if mod_perms.can_untimeout:
                visible.append(("untimeout", MODERATION_HELP_DESCRIPTIONS["untimeout"]))
            if mod_perms.can_ban:
                visible.append(("ban", MODERATION_HELP_DESCRIPTIONS["ban"]))
            if mod_perms.can_whisper:
                visible.append(("whisper", MODERATION_HELP_DESCRIPTIONS["whisper"]))
            if mod_perms.can_warn:
                visible.append(("warn", MODERATION_HELP_DESCRIPTIONS["warn"]))
                visible.append(("note", MODERATION_HELP_DESCRIPTIONS["note"]))
            if mod_perms.can_view_warnings:
                visible.append(("warnings", MODERATION_HELP_DESCRIPTIONS["warnings"]))
            if mod_perms.can_check:
                visible.append(("check", MODERATION_HELP_DESCRIPTIONS["check"]))
                visible.append(("history", MODERATION_HELP_DESCRIPTIONS["history"]))
            if mod_perms.can_manage_settings:
                visible.append(("clearcheck", MODERATION_HELP_DESCRIPTIONS["clearcheck"]))
                visible.append(("settings", MODERATION_HELP_DESCRIPTIONS["settings"]))
                visible.append(("word", MODERATION_HELP_DESCRIPTIONS["word"]))
                visible.append(("rolesweep", MODERATION_HELP_DESCRIPTIONS["rolesweep"]))

        await interaction.response.send_message(
            embed=help_embed(visible, command_channel), ephemeral=True
        )

    @app_commands.default_permissions(administrator=True)
    async def authcheck_command(self, interaction: discord.Interaction) -> None:
        if not await self._require_administrator(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            result = await self._monitor().probe_authentication()
            await interaction.followup.send(embed=authcheck_embed(result), ephemeral=True)
        except Exception:
            LOGGER.exception("The /authcheck command failed")
            await interaction.followup.send(
                "OctoBot could not complete the authentication diagnostic.",
                ephemeral=True,
            )

    async def config_role_add_command(
        self,
        interaction: discord.Interaction,
        command: CommandChoice,
        role: discord.Role,
    ) -> None:
        if not await self._require_administrator(interaction):
            return
        added = await self.database.add_command_role(
            self._guild_id(interaction), command, role.id, interaction.user.id
        )
        message = (
            f"Added {role.mention} to `/{command}`."
            if added
            else f"{role.mention} already has access to `/{command}`."
        )
        await interaction.response.send_message(message, ephemeral=True)

    async def config_role_remove_command(
        self,
        interaction: discord.Interaction,
        command: CommandChoice,
        role_id: str,
    ) -> None:
        if not await self._require_administrator(interaction):
            return
        try:
            parsed_role_id = int(role_id.strip())
            if parsed_role_id <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "Role ID must be a positive Discord numeric ID.", ephemeral=True
            )
            return
        removed = await self.database.remove_command_role(
            self._guild_id(interaction), command, parsed_role_id
        )
        role = interaction.guild.get_role(parsed_role_id) if interaction.guild else None
        label = role.mention if role else f"deleted role ID {parsed_role_id}"
        message = (
            f"Removed {label} from `/{command}`."
            if removed
            else f"That role ID was not configured for `/{command}`."
        )
        await interaction.response.send_message(message, ephemeral=True)

    async def config_role_list_command(self, interaction: discord.Interaction) -> None:
        if not await self._require_administrator(interaction):
            return
        configured = await self.database.all_command_roles(self._guild_id(interaction))
        known_role_ids = (
            {role.id for role in interaction.guild.roles}
            if interaction.guild is not None
            else set()
        )
        lines = []
        for command in MANAGED_COMMANDS:
            role_ids = configured[command]
            labels = [
                command_role_label(role_id, known_role_ids)
                for role_id in sorted(role_ids)
            ]
            lines.append(f"`/{command}`: {', '.join(labels) if labels else 'Open'}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def _refresh_dashboard_after_report_change(self) -> None:
        try:
            await self._monitor().refresh_status_from_database()
        except Exception:
            LOGGER.exception("Could not refresh dashboard after report change")

    async def _has_command_access(
        self, interaction: discord.Interaction, command_name: str
    ) -> bool:
        allowed_role_ids = await self.database.command_roles(
            self._guild_id(interaction), command_name
        )
        user_role_ids = {role.id for role in getattr(interaction.user, "roles", ())}
        return command_access_allowed(
            allowed_role_ids,
            user_role_ids,
            administrator=self._is_administrator(interaction),
        )

    async def _require_command_access(
        self, interaction: discord.Interaction, command_name: str
    ) -> bool:
        if await self._has_command_access(interaction, command_name):
            return True
        await interaction.response.send_message(
            "You do not have a configured role for this command.", ephemeral=True
        )
        return False

    async def _require_administrator(self, interaction: discord.Interaction) -> bool:
        if self._is_administrator(interaction):
            return True
        await interaction.response.send_message(
            "This command requires the Discord Administrator permission.",
            ephemeral=True,
        )
        return False

    async def _require_moderator(self, interaction: discord.Interaction) -> bool:
        if self._is_moderator(interaction):
            return True
        await interaction.response.send_message(
            "This command requires Administrator, Manage Messages, or "
            "Moderate Members permission.",
            ephemeral=True,
        )
        return False

    @staticmethod
    def _is_administrator(interaction: discord.Interaction) -> bool:
        guild_permissions = getattr(interaction.user, "guild_permissions", None)
        if guild_permissions is not None and guild_permissions.administrator:
            return True
        permissions = getattr(interaction, "permissions", None)
        return bool(permissions and permissions.administrator)

    @staticmethod
    def _is_moderator(interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        return bool(
            permissions
            and (
                permissions.administrator
                or permissions.manage_messages
                or permissions.moderate_members
            )
        )

    def _guild_id(self, interaction: discord.Interaction) -> int:
        return interaction.guild_id or self.config.guild_id

    def _monitor(self) -> Monitor:
        if self.monitor is None:
            raise RuntimeError("Monitor has not been initialized")
        return self.monitor

    def _radio_monitor(self) -> RadioMonitor:
        if self.radio_monitor is None:
            raise RuntimeError("Radio monitor has not been initialized")
        return self.radio_monitor


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()
    bot = OctoBot(config)
    bot.run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
