from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands

from ..duration import DurationError, format_duration, parse_duration

if TYPE_CHECKING:
    from octobot.bot import OctoBot

log = logging.getLogger(__name__)

LOCKDOWN_DM = "We are currently in Lockdown. Please try again soon"
LOCKDOWN_BAN_DAYS = 7
DEFAULT_LOCKDOWN_SECONDS = 30 * 60
MAX_LOCKDOWN_SECONDS = 7 * 24 * 60 * 60
# Starting a lockdown also bans everyone who joined this recently: a raid is
# usually noticed a few minutes in, and those accounts are already inside.
RECENT_JOIN_WINDOW = timedelta(minutes=10)
SWEEP_SECONDS = 10


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LockdownCog(
    commands.GroupCog,
    group_name="lockdown",
    group_description="Raid protection: temporarily ban everyone who joins.",
):
    """While lockdown is on, every new joiner is DMed and banned for 7 days.

    Lockdown switches itself off after the configured timer, and the bot lifts
    each lockdown ban when its 7 days are up. Both survive a restart because the
    state lives in the database.
    """

    def __init__(self, bot: "OctoBot"):
        self.bot = bot
        self._task: asyncio.Task[None] | None = None

    async def cog_load(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="lockdown-sweep")

    async def cog_unload(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    # ------------------------------------------------------------------ helpers

    @property
    def _db(self):
        return self.bot.moderation_database

    async def _state(self, guild_id: int) -> tuple[datetime | None, int]:
        """(lockdown end time or None, configured duration in seconds)."""
        settings = await self._db.get_guild_settings(guild_id)
        until = settings.get("lockdown_until")
        until_dt = datetime.fromisoformat(until) if until else None
        seconds = int(settings.get("lockdown_seconds") or DEFAULT_LOCKDOWN_SECONDS)
        return until_dt, seconds

    async def is_active(self, guild_id: int, now: datetime | None = None) -> bool:
        until, _ = await self._state(guild_id)
        return until is not None and until > (now or utc_now())

    async def _log(self, guild: discord.Guild, embed: discord.Embed) -> None:
        moderation = self.bot.get_cog("ModerationCog")
        if moderation is not None:
            await moderation._log(guild, embed)

    async def _require_manage(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Member, discord.Guild] | None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return None
        perms = await self.bot.moderation_permissions.for_member(interaction.user)
        if not perms.can_manage_settings:
            await interaction.response.send_message(
                "You do not have permission to manage lockdown.", ephemeral=True
            )
            return None
        return interaction.user, interaction.guild

    # ------------------------------------------------------------------ enforcement

    async def handle_join(self, member: discord.Member, now: datetime | None = None) -> bool:
        """Ban a joiner if lockdown is on. Returns True when a ban was issued."""
        now = now or utc_now()
        if member.guild.id != self.bot.config.guild_id or member.bot:
            return False
        if not await self.is_active(member.guild.id, now):
            return False
        return await self._ban_for_lockdown(member, now, "joined during lockdown")

    async def _is_staff(self, member: discord.Member) -> bool:
        if member.id == member.guild.owner_id or member.guild_permissions.administrator:
            return True
        perms = await self.bot.moderation_permissions.for_member(member)
        return any((perms.can_timeout, perms.can_warn, perms.can_check, perms.can_manage_settings))

    async def ban_recent_joiners(self, guild: discord.Guild, now: datetime) -> int:
        """Ban members who joined within RECENT_JOIN_WINDOW. Returns how many were banned."""
        cutoff = now - RECENT_JOIN_WINDOW
        banned = 0
        for member in list(guild.members):
            joined = getattr(member, "joined_at", None)
            if member.bot or joined is None or joined < cutoff:
                continue
            if await self._is_staff(member):
                continue
            if await self._ban_for_lockdown(member, now, "joined in the 10 minutes before lockdown"):
                banned += 1
        return banned

    async def _ban_for_lockdown(self, member: discord.Member, now: datetime, why: str) -> bool:
        dm_sent = True
        try:
            await member.send(LOCKDOWN_DM)
        except (discord.Forbidden, discord.HTTPException):
            dm_sent = False

        unban_at = now + timedelta(days=LOCKDOWN_BAN_DAYS)
        try:
            await member.guild.ban(
                member,
                reason=f"OctoBot lockdown | auto-ban until {unban_at:%Y-%m-%d %H:%M} UTC",
                delete_message_seconds=0,
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Lockdown could not ban joiner %s", member.id)
            return False

        await self._db.add_lockdown_ban(member.guild.id, member.id, now, unban_at)
        embed = discord.Embed(title="Lockdown: joiner banned", description=why)
        embed.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="Account created", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)
        embed.add_field(name="Unban", value=f"<t:{int(unban_at.timestamp())}:R>", inline=True)
        embed.add_field(name="DM", value="sent" if dm_sent else "not delivered", inline=True)
        await self._log(member.guild, embed)
        return True

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        try:
            await self.handle_join(member)
        except Exception:
            log.exception("Lockdown join handling failed for %s", getattr(member, "id", None))

    async def _loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Lockdown sweep failed")
            await asyncio.sleep(SWEEP_SECONDS)

    async def sweep(self, now: datetime | None = None) -> None:
        """Turn an expired lockdown off and lift lockdown bans that have served 7 days."""
        now = now or utc_now()
        guild = self.bot.get_guild(self.bot.config.guild_id)
        if guild is None:
            return
        until, _ = await self._state(guild.id)
        if until is not None and until <= now:
            await self._db.set_lockdown_until(guild.id, None)
            count = await self._db.count_lockdown_bans_since(guild.id, until - timedelta(seconds=MAX_LOCKDOWN_SECONDS))
            embed = discord.Embed(title="Lockdown ended", description="The timer ran out.")
            embed.add_field(name="Joiners banned during lockdown", value=str(count), inline=True)
            await self._log(guild, embed)
            log.info("Lockdown ended automatically")

        for row in await self._db.expired_lockdown_bans(guild.id, now):
            user = discord.Object(id=int(row["user_id"]))
            try:
                await guild.unban(user, reason="OctoBot lockdown | 7-day auto-ban expired")
            except discord.NotFound:
                pass  # Already unbanned by staff.
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Lockdown could not unban %s", row["user_id"])
                continue
            await self._db.mark_lockdown_unbanned(guild.id, int(row["user_id"]), now)

    # ------------------------------------------------------------------ commands

    async def _end_now(
        self, interaction: discord.Interaction, actor: discord.Member, guild: discord.Guild, how: str
    ) -> None:
        now = utc_now()
        until, seconds = await self._state(guild.id)
        if until is None or until <= now:
            await interaction.response.send_message("Lockdown is already off.", ephemeral=True)
            return
        await self._db.set_lockdown_until(guild.id, None)
        count = await self._db.count_lockdown_bans_since(guild.id, until - timedelta(seconds=seconds))
        remaining = format_duration(max(1, int((until - now).total_seconds())))
        await interaction.response.send_message(
            f"🔓 Lockdown {how} with **{remaining}** left on the timer. {count} joiner(s) were banned "
            f"during it; each is unbanned automatically after {LOCKDOWN_BAN_DAYS} days.",
            ephemeral=True,
        )
        embed = discord.Embed(title=f"Lockdown {how}", description=f"{remaining} was left on the timer.")
        embed.add_field(name="Joiners banned during lockdown", value=str(count), inline=True)
        embed.add_field(name="By", value=actor.mention, inline=True)
        await self._log(guild, embed)

    @app_commands.command(name="toggle", description="Turn lockdown on (for the configured timer) or off.")
    @app_commands.describe(state="on starts a lockdown for the configured timer; off ends it now")
    @app_commands.guild_only()
    async def toggle(self, interaction: discord.Interaction, state: Literal["on", "off"]) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        actor, guild = context
        if state == "off":
            await self._end_now(interaction, actor, guild, "turned off")
            return

        now = utc_now()
        until, seconds = await self._state(guild.id)
        active = until is not None and until > now
        new_until = now + timedelta(seconds=seconds)
        await self._db.set_lockdown_until(guild.id, new_until)
        verb = "extended" if active else "started"

        # Banning recent joiners can take a while on a busy raid; acknowledge first.
        await interaction.response.defer(ephemeral=True, thinking=True)
        retro = 0
        if not active:
            retro = await self.ban_recent_joiners(guild, now)
        retro_note = (
            f" Also banned **{retro}** member(s) who joined in the last "
            f"{int(RECENT_JOIN_WINDOW.total_seconds() // 60)} minutes."
            if not active else ""
        )
        await interaction.followup.send(
            f"🔒 Lockdown {verb} for **{format_duration(seconds)}**. Everyone who joins is banned for "
            f"{LOCKDOWN_BAN_DAYS} days until <t:{int(new_until.timestamp())}:t> "
            f"(<t:{int(new_until.timestamp())}:R>).{retro_note} `/lockdown cancel` stops it early.",
            ephemeral=True,
        )
        embed = discord.Embed(title=f"Lockdown {verb}", colour=discord.Colour.red())
        embed.add_field(name="Ends", value=f"<t:{int(new_until.timestamp())}:F>", inline=True)
        embed.add_field(name="By", value=actor.mention, inline=True)
        if not active:
            embed.add_field(name="Recent joiners banned", value=str(retro), inline=True)
        await self._log(guild, embed)

    @app_commands.command(name="cancel", description="Stop the current lockdown before its timer runs out.")
    @app_commands.guild_only()
    async def cancel(self, interaction: discord.Interaction) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        actor, guild = context
        await self._end_now(interaction, actor, guild, "cancelled")

    @app_commands.command(name="timer", description="Set how long a lockdown lasts before turning itself off.")
    @app_commands.describe(duration="Duration using s, m, h, d (examples: 90s, 30m, 2h, 1d, 1h30m). Max 7d.")
    @app_commands.guild_only()
    async def timer(self, interaction: discord.Interaction, duration: str) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        _, guild = context
        try:
            seconds = parse_duration(duration, maximum=MAX_LOCKDOWN_SECONDS)
        except DurationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self._db.set_lockdown_seconds(guild.id, seconds)
        until, _ = await self._state(guild.id)
        note = ""
        if until is not None and until > utc_now():
            note = " The lockdown running now keeps its current end time; use `/lockdown toggle on` to restart it with the new timer."
        await interaction.response.send_message(
            f"Lockdown timer set to **{format_duration(seconds)}**.{note}",
            ephemeral=True,
        )

    @app_commands.command(name="status", description="Show whether lockdown is on and how many joiners it has banned.")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        _, guild = context
        now = utc_now()
        until, seconds = await self._state(guild.id)
        active = until is not None and until > now
        currently_banned = await self._db.count_active_lockdown_bans(guild.id, now)
        total = await self._db.count_lockdown_bans_since(guild.id, None)

        embed = discord.Embed(
            title="🔒 Lockdown is ON" if active else "🔓 Lockdown is off",
            colour=discord.Colour.red() if active else discord.Colour.green(),
        )
        if active:
            embed.add_field(name="Ends", value=f"<t:{int(until.timestamp())}:t> (<t:{int(until.timestamp())}:R>)", inline=True)
            banned_this = await self._db.count_lockdown_bans_since(guild.id, until - timedelta(seconds=seconds))
            embed.add_field(name="Banned this lockdown", value=str(banned_this), inline=True)
        embed.add_field(name="Timer", value=format_duration(seconds), inline=True)
        embed.add_field(name="Still banned (auto-unban pending)", value=str(currently_banned), inline=True)
        embed.add_field(name="Banned by lockdown, all time", value=str(total), inline=True)
        embed.set_footer(text=f"Joiners are banned for {LOCKDOWN_BAN_DAYS} days and unbanned automatically")
        await interaction.response.send_message(embed=embed, ephemeral=True)
