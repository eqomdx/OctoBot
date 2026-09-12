from __future__ import annotations

from datetime import datetime

import discord

from .duration import format_duration


def utc_timestamp(iso_value: str) -> int:
    return int(datetime.fromisoformat(iso_value).timestamp())


def warning_embed(
    *, user: discord.Member, moderator_id: int, reason: str, warning_id: int
) -> discord.Embed:
    embed = discord.Embed(title="Warning issued", description=reason)
    embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=False)
    embed.add_field(name="Moderator", value=f"<@{moderator_id}>", inline=True)
    embed.add_field(name="Case", value=f"W-{warning_id:04d}", inline=True)
    return embed


def timeout_embed(
    *,
    user: discord.Member,
    moderator_id: int,
    reason: str,
    timeout_id: int,
    duration_seconds: int,
    expires_at: datetime,
) -> discord.Embed:
    embed = discord.Embed(title="User timed out", description=reason)
    embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=False)
    embed.add_field(name="Moderator", value=f"<@{moderator_id}>", inline=True)
    embed.add_field(name="Duration", value=format_duration(duration_seconds), inline=True)
    embed.add_field(name="Expires", value=f"<t:{int(expires_at.timestamp())}:F>", inline=False)
    embed.add_field(name="Case", value=f"T-{timeout_id:04d}", inline=True)
    return embed


def untimeout_embed(
    *, user: discord.Member, moderator_id: int, reason: str, timeout_id: int | None
) -> discord.Embed:
    embed = discord.Embed(title="Timeout removed", description=reason)
    embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=False)
    embed.add_field(name="Moderator", value=f"<@{moderator_id}>", inline=True)
    if timeout_id is not None:
        embed.add_field(name="Timeout case", value=f"T-{timeout_id:04d}", inline=True)
    return embed
