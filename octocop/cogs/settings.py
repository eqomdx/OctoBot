from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands

from ..database import RolePermissions
from ..duration import DurationError, MAX_TIMEOUT_SECONDS, format_duration, parse_duration

if TYPE_CHECKING:
    from octobot.bot import OctoBot


PERMISSION_CHOICES = [
    app_commands.Choice(name="Timeout users", value="timeout"),
    app_commands.Choice(name="Warn users", value="warn"),
    app_commands.Choice(name="View warnings", value="warnings"),
    app_commands.Choice(name="Check moderation history", value="check"),
    app_commands.Choice(name="Remove timeouts", value="untimeout"),
    app_commands.Choice(name="Ban users", value="ban"),
    app_commands.Choice(name="Manage bot settings", value="settings"),
]


class SettingsCog(
    commands.GroupCog,
    group_name="settings",
    group_description="Configure OctoBot permissions and server settings.",
):
    def __init__(self, bot: "OctoBot"):
        self.bot = bot

    async def _context(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Member, discord.Guild] | None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return None
        return interaction.user, interaction.guild

    async def _can_manage(self, actor: discord.Member) -> bool:
        perms = await self.bot.moderation_permissions.for_member(actor)
        return perms.can_manage_settings

    async def _require_manage(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Member, discord.Guild] | None:
        context = await self._context(interaction)
        if context is None:
            return None
        actor, guild = context
        if not await self._can_manage(actor):
            await interaction.response.send_message(
                "You do not have permission to manage OctoBot settings.", ephemeral=True
            )
            return None
        return actor, guild

    @app_commands.command(name="bootstrap", description="Set the initial Helper/Moderator/Admin permission profiles.")
    @app_commands.describe(
        helper="Helper role: timeout only, maximum 1 hour",
        moderator="Moderator role: all bot permissions, maximum 28 days",
        log_channel="Channel for automatic moderation logs",
        admin="Optional Admin role: all bot permissions, maximum 28 days",
    )
    @app_commands.guild_only()
    async def bootstrap(
        self,
        interaction: discord.Interaction,
        helper: discord.Role,
        moderator: discord.Role,
        log_channel: discord.TextChannel,
        admin: discord.Role | None = None,
    ) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        _, guild = context

        helper_profile = RolePermissions(
            guild_id=guild.id,
            role_id=helper.id,
            can_timeout=True,
            max_timeout_seconds=3600,
        )
        moderator_profile = RolePermissions(
            guild_id=guild.id,
            role_id=moderator.id,
            can_timeout=True,
            can_warn=True,
            can_view_warnings=True,
            can_check=True,
            can_untimeout=True,
            can_ban=True,
            can_manage_settings=True,
            max_timeout_seconds=MAX_TIMEOUT_SECONDS,
        )
        await self.bot.moderation_database.upsert_role_profile(helper_profile)
        await self.bot.moderation_database.upsert_role_profile(moderator_profile)
        if admin is not None:
            await self.bot.moderation_database.upsert_role_profile(
                RolePermissions(
                    guild_id=guild.id,
                    role_id=admin.id,
                    can_timeout=True,
                    can_warn=True,
                    can_view_warnings=True,
                    can_check=True,
                    can_untimeout=True,
                    can_ban=True,
                    can_manage_settings=True,
                    max_timeout_seconds=MAX_TIMEOUT_SECONDS,
                )
            )
        await self.bot.moderation_database.set_log_channel(guild.id, log_channel.id)

        admin_line = f"\n**Admin:** {admin.mention} — full access" if admin else ""
        await interaction.response.send_message(
            f"OctoBot configured.\n"
            f"**Helper:** {helper.mention} — `/timeout` only, max `1h`\n"
            f"**Moderator:** {moderator.mention} — full access, max `4w`"
            f"{admin_line}\n"
            f"**Mod log:** {log_channel.mention}",
            ephemeral=True,
        )

    @app_commands.command(name="permission", description="Allow or deny one bot capability for a Discord role.")
    @app_commands.describe(role="Role to configure", permission="Capability", access="Allow or deny")
    @app_commands.choices(permission=PERMISSION_CHOICES)
    @app_commands.guild_only()
    async def permission(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        permission: app_commands.Choice[str],
        access: Literal["allow", "deny"],
    ) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        _, guild = context
        enabled = access == "allow"
        await self.bot.moderation_database.set_role_permission(guild.id, role.id, permission.value, enabled)
        await interaction.response.send_message(
            f"**{permission.name}** for {role.mention}: **{'allowed' if enabled else 'denied'}**.",
            ephemeral=True,
        )

    @app_commands.command(name="timeout-limit", description="Set the maximum timeout duration a role can issue.")
    @app_commands.describe(role="Role to configure", duration="Maximum duration, e.g. 1h, 2d, 1w")
    @app_commands.guild_only()
    async def timeout_limit(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        duration: str,
    ) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        _, guild = context
        try:
            seconds = parse_duration(duration)
        except DurationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.bot.moderation_database.set_role_timeout_limit(guild.id, role.id, seconds)
        await interaction.response.send_message(
            f"{role.mention}'s timeout limit is now **{format_duration(seconds)}**.", ephemeral=True
        )

    @app_commands.command(name="log-channel", description="Set or change the automatic moderation log channel.")
    @app_commands.describe(channel="Channel where moderation actions should be logged")
    @app_commands.guild_only()
    async def log_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        _, guild = context
        await self.bot.moderation_database.set_log_channel(guild.id, channel.id)
        await interaction.response.send_message(
            f"Moderation logs will now be sent to {channel.mention}.", ephemeral=True
        )

    @app_commands.command(
        name="timeout-cleanup",
        description="Set how many minutes of a timed-out user's recent messages are deleted.",
    )
    @app_commands.describe(
        minutes="Minutes of recent messages to delete after a timeout. Use 0 to disable (max 1440)."
    )
    @app_commands.guild_only()
    async def timeout_cleanup(self, interaction: discord.Interaction, minutes: int) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        _, guild = context
        if minutes < 0 or minutes > 1440:
            await interaction.response.send_message(
                "Cleanup minutes must be between **0** and **1440** (24 hours).", ephemeral=True
            )
            return
        await self.bot.moderation_database.set_timeout_cleanup_minutes(guild.id, minutes)
        if minutes == 0:
            message = "Automatic message cleanup after timeouts is now **disabled**."
        else:
            message = (
                f"After each successful timeout, OctoBot will delete that user's messages "
                f"from the previous **{minutes} minute{'s' if minutes != 1 else ''}** wherever it has permission."
            )
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="dm-warnings", description="Enable or disable warning DMs to users.")
    @app_commands.guild_only()
    async def dm_warnings(self, interaction: discord.Interaction, enabled: bool) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        _, guild = context
        await self.bot.moderation_database.set_dm_setting(guild.id, "dm_warnings", enabled)
        await interaction.response.send_message(
            f"Warning DMs are now **{'enabled' if enabled else 'disabled'}**.", ephemeral=True
        )

    @app_commands.command(name="dm-timeouts", description="Enable or disable timeout DMs to users.")
    @app_commands.guild_only()
    async def dm_timeouts(self, interaction: discord.Interaction, enabled: bool) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        _, guild = context
        await self.bot.moderation_database.set_dm_setting(guild.id, "dm_timeouts", enabled)
        await interaction.response.send_message(
            f"Timeout DMs are now **{'enabled' if enabled else 'disabled'}**.", ephemeral=True
        )

    @app_commands.command(name="dm-bans", description="Enable or disable ban DMs to users.")
    @app_commands.guild_only()
    async def dm_bans(self, interaction: discord.Interaction, enabled: bool) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        _, guild = context
        await self.bot.moderation_database.set_dm_setting(guild.id, "dm_bans", enabled)
        await interaction.response.send_message(
            f"Ban DMs are now **{'enabled' if enabled else 'disabled'}**.", ephemeral=True
        )

    @app_commands.command(name="view", description="View OctoBot permissions configured for a role.")
    @app_commands.describe(role="Role to inspect")
    @app_commands.guild_only()
    async def view(self, interaction: discord.Interaction, role: discord.Role) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        _, guild = context
        profile = await self.bot.moderation_database.get_role_profile(guild.id, role.id)
        if profile is None:
            await interaction.response.send_message(
                f"{role.mention} has no OctoBot profile. It therefore has no bot permissions.",
                ephemeral=True,
            )
            return

        def mark(value: bool) -> str:
            return "Yes" if value else "No"

        embed = discord.Embed(title=f"OctoBot permissions — {role.name}")
        embed.add_field(name="Timeout users", value=mark(profile.can_timeout), inline=True)
        embed.add_field(name="Timeout limit", value=format_duration(profile.max_timeout_seconds), inline=True)
        embed.add_field(name="Warn users", value=mark(profile.can_warn), inline=True)
        embed.add_field(name="View warnings", value=mark(profile.can_view_warnings), inline=True)
        embed.add_field(name="Check history", value=mark(profile.can_check), inline=True)
        embed.add_field(name="Remove timeouts", value=mark(profile.can_untimeout), inline=True)
        embed.add_field(name="Ban users", value=mark(profile.can_ban), inline=True)
        embed.add_field(name="Manage settings", value=mark(profile.can_manage_settings), inline=True)
        embed.set_footer(text=f"Role ID: {role.id}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
