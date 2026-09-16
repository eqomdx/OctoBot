from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from octobot.bot import OctoBot

log = logging.getLogger(__name__)


class RoleCleanupCog(commands.Cog):
    """Drop a legacy role from members who hold one of the roles that supersede it.

    Two paths share the same removal:
    - automatic: whenever a member gains one of ``role_cleanup_auto_trigger_role_ids``
    - ``/rolesweep``: a one-off pass over every member, using the wider
      ``role_cleanup_sweep_trigger_role_ids`` list
    """

    def __init__(self, bot: "OctoBot"):
        self.bot = bot

    @property
    def _remove_role_id(self) -> int | None:
        return self.bot.config.role_cleanup_remove_role_id

    @staticmethod
    def _has_any(member: discord.Member, role_ids: frozenset[int]) -> bool:
        return any(role.id in role_ids for role in member.roles)

    async def _remove_legacy_role(self, member: discord.Member, reason: str) -> bool:
        """Remove the legacy role from ``member`` if present. True when removed."""
        remove_id = self._remove_role_id
        if remove_id is None:
            return False
        role = member.guild.get_role(remove_id)
        if role is None or role not in member.roles:
            return False
        await member.remove_roles(role, reason=reason[:512])
        return True

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.guild.id != self.bot.config.guild_id:
            return
        triggers = self.bot.config.role_cleanup_auto_trigger_role_ids
        if not triggers or self._remove_role_id is None:
            return
        if not self._has_any(after, triggers):
            return
        before_ids = {role.id for role in before.roles}
        after_ids = {role.id for role in after.roles}
        # Only act on the change that granted a trigger role (or re-added the legacy
        # role while a trigger role is held). Unrelated updates are ignored.
        gained_trigger = bool((after_ids - before_ids) & triggers)
        regained_legacy = self._remove_role_id in after_ids - before_ids
        if not (gained_trigger or regained_legacy):
            return
        try:
            removed = await self._remove_legacy_role(
                after, "OctoBot role cleanup | superseded by a higher role"
            )
        except discord.Forbidden:
            log.warning(
                "Cannot remove role %s from %s: missing Manage Roles or role is above mine",
                self._remove_role_id,
                after.id,
            )
            return
        except discord.HTTPException:
            log.exception("Failed to remove role %s from %s", self._remove_role_id, after.id)
            return
        if removed:
            log.info("Removed legacy role %s from %s", self._remove_role_id, after.id)

    @app_commands.command(
        name="rolesweep",
        description="One-off: remove the legacy role from every member who holds a superseding role.",
    )
    @app_commands.guild_only()
    async def rolesweep_command(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        perms = await self.bot.moderation_permissions.for_member(interaction.user)
        if not perms.can_manage_settings:
            await interaction.response.send_message(
                "You do not have permission to use `/rolesweep`.", ephemeral=True
            )
            return

        guild = interaction.guild
        remove_id = self._remove_role_id
        triggers = self.bot.config.role_cleanup_sweep_trigger_role_ids
        if remove_id is None or not triggers:
            await interaction.response.send_message(
                "Role cleanup is not configured (ROLE_CLEANUP_* in .env).", ephemeral=True
            )
            return
        legacy = guild.get_role(remove_id)
        if legacy is None:
            await interaction.response.send_message(
                f"Role `{remove_id}` does not exist in this server.", ephemeral=True
            )
            return

        # A full member walk can take a while on a large server; acknowledge first.
        await interaction.response.defer(ephemeral=True, thinking=True)
        checked = 0
        removed = 0
        failed = 0
        try:
            async for member in guild.fetch_members(limit=None):
                checked += 1
                if legacy not in member.roles or not self._has_any(member, triggers):
                    continue
                try:
                    await member.remove_roles(
                        legacy, reason=f"OctoBot /rolesweep by {interaction.user} ({interaction.user.id})"[:512]
                    )
                    removed += 1
                except (discord.Forbidden, discord.HTTPException):
                    failed += 1
                    log.exception("Failed to remove %s from %s during /rolesweep", legacy.id, member.id)
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"Discord rejected the member list: `{exc}`. Check that the **Server Members Intent** is enabled.",
                ephemeral=True,
            )
            return

        trigger_mentions = ", ".join(f"<@&{role_id}>" for role_id in sorted(triggers))
        await interaction.followup.send(
            f"Checked **{checked}** members. Removed {legacy.mention} from **{removed}** member(s) "
            f"holding any of {trigger_mentions}."
            + (f" **{failed}** removal(s) failed; see the bot log." if failed else ""),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
