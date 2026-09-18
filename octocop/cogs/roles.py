from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from octobot.bot import OctoBot

log = logging.getLogger(__name__)


class RoleCleanupCog(commands.Cog):
    """Drop the legacy role from a member the moment they gain a role that supersedes it.

    Triggered by member role updates only (``role_cleanup_auto_trigger_role_ids``).
    The one-off ``/rolesweep`` pass that migrated existing members has been removed.
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
