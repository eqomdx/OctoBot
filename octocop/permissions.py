from __future__ import annotations

from dataclasses import dataclass

import discord

from .database import Database, RolePermissions
from .duration import MAX_TIMEOUT_SECONDS


@dataclass(slots=True)
class EffectivePermissions:
    can_timeout: bool = False
    can_warn: bool = False
    can_view_warnings: bool = False
    can_check: bool = False
    can_untimeout: bool = False
    can_ban: bool = False
    can_manage_settings: bool = False
    max_timeout_seconds: int = 0
    source_role_id: int | None = None

    @classmethod
    def full(cls) -> "EffectivePermissions":
        return cls(
            can_timeout=True,
            can_warn=True,
            can_view_warnings=True,
            can_check=True,
            can_untimeout=True,
            can_ban=True,
            can_manage_settings=True,
            max_timeout_seconds=MAX_TIMEOUT_SECONDS,
        )


class PermissionService:
    def __init__(self, database: Database):
        self.database = database

    async def for_member(self, member: discord.Member) -> EffectivePermissions:
        guild = member.guild
        if member.id == guild.owner_id or member.guild_permissions.administrator:
            return EffectivePermissions.full()

        profiles = await self.database.get_role_profiles(
            guild.id, (role.id for role in member.roles if not role.is_default())
        )
        if not profiles:
            return EffectivePermissions()

        # The highest configured Discord role wins. This makes overlapping staff roles predictable.
        configured_roles = [role for role in member.roles if role.id in profiles]
        source_role = max(configured_roles, key=lambda r: r.position)
        profile: RolePermissions = profiles[source_role.id]
        return EffectivePermissions(
            can_timeout=profile.can_timeout,
            can_warn=profile.can_warn,
            can_view_warnings=profile.can_view_warnings,
            can_check=profile.can_check,
            can_untimeout=profile.can_untimeout,
            can_ban=profile.can_ban,
            can_manage_settings=profile.can_manage_settings,
            max_timeout_seconds=profile.max_timeout_seconds if profile.can_timeout else 0,
            source_role_id=profile.role_id,
        )


def moderation_target_error(actor: discord.Member, target: discord.Member, bot_member: discord.Member) -> str | None:
    guild = actor.guild
    if actor.id == target.id:
        return "You cannot moderate yourself."
    if target.id == guild.owner_id:
        return "The server owner cannot be moderated by this bot."
    if target.bot:
        return "This bot is configured to moderate human members only."
    if target.guild_permissions.administrator:
        return "Discord does not allow this bot to time out an Administrator."
    if bot_member.top_role <= target.top_role:
        return "My Discord role must be above the target user's highest role."
    if actor.id != guild.owner_id and not actor.guild_permissions.administrator:
        if actor.top_role <= target.top_role:
            return "You cannot moderate a member with an equal or higher Discord role than your own."
    return None


def ban_target_error(
    actor: discord.Member, target: discord.abc.User, bot_member: discord.Member
) -> str | None:
    """Like moderation_target_error, but for /ban.

    The target may be a plain User (already left the server), in which case only the
    identity checks apply because there is no role hierarchy to compare.
    """
    guild = actor.guild
    if actor.id == target.id:
        return "You cannot ban yourself."
    if target.id == guild.owner_id:
        return "The server owner cannot be banned by this bot."
    if target.bot:
        return "This bot is configured to moderate human members only."
    if isinstance(target, discord.Member):
        if bot_member.top_role <= target.top_role:
            return "My Discord role must be above the target user's highest role."
        if actor.id != guild.owner_id and not actor.guild_permissions.administrator:
            if actor.top_role <= target.top_role:
                return "You cannot ban a member with an equal or higher Discord role than your own."
    return None
