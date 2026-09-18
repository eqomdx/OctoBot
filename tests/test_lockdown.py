from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from octocop.cogs.lockdown import LOCKDOWN_BAN_DAYS, LOCKDOWN_DM, LockdownCog
from octocop.database import Database

GUILD = 123


class _Guild:
    def __init__(self):
        self.id = GUILD
        self.owner_id = 0
        self.members: list = []
        self.bans: list[tuple[int, str]] = []
        self.unbans: list[int] = []

    async def ban(self, user, *, reason=None, delete_message_seconds=0):
        self.bans.append((user.id, reason))

    async def unban(self, user, *, reason=None):
        self.unbans.append(user.id)


class _Member:
    def __init__(self, guild: _Guild, member_id: int, *, bot: bool = False, dm_ok: bool = True):
        self.guild = guild
        self.id = member_id
        self.bot = bot
        self.mention = f"<@{member_id}>"
        self.created_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.joined_at = None
        self.guild_permissions = SimpleNamespace(administrator=False)
        self.dms: list[str] = []
        self._dm_ok = dm_ok

    async def send(self, content):
        if not self._dm_ok:
            import discord
            raise discord.Forbidden(SimpleNamespace(status=403, reason="closed"), "closed")
        self.dms.append(content)


class _Perms:
    def __init__(self, staff_ids: set[int]):
        self.staff_ids = staff_ids

    async def for_member(self, member):
        staff = member.id in self.staff_ids
        return SimpleNamespace(can_timeout=staff, can_warn=staff, can_check=staff, can_manage_settings=staff)


class _Bot:
    def __init__(self, database: Database, guild: _Guild):
        self.moderation_database = database
        self.moderation_permissions = _Perms({42})
        self.config = SimpleNamespace(guild_id=GUILD)
        self._guild = guild
        self.logged = []

    def get_guild(self, guild_id):
        return self._guild if guild_id == GUILD else None

    def get_cog(self, name):
        bot = self

        class _Mod:
            async def _log(self, guild, embed):
                bot.logged.append(embed.title)

        return _Mod() if name == "ModerationCog" else None


class LockdownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "octobot.db")
        await self.db.connect()
        self.guild = _Guild()
        self.bot = _Bot(self.db, self.guild)
        self.cog = LockdownCog(self.bot)
        self.now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp.cleanup()

    async def test_joiner_is_ignored_when_lockdown_is_off(self) -> None:
        member = _Member(self.guild, 1)
        self.assertFalse(await self.cog.handle_join(member, self.now))
        self.assertEqual(self.guild.bans, [])
        self.assertEqual(member.dms, [])

    async def test_joiner_is_dmed_then_banned_for_seven_days_during_lockdown(self) -> None:
        await self.db.set_lockdown_until(GUILD, self.now + timedelta(minutes=10))
        member = _Member(self.guild, 1)
        self.assertTrue(await self.cog.handle_join(member, self.now))
        self.assertEqual(member.dms, [LOCKDOWN_DM])
        self.assertEqual(len(self.guild.bans), 1)
        self.assertEqual(self.guild.bans[0][0], 1)
        self.assertIn("lockdown", self.guild.bans[0][1])
        self.assertEqual(await self.db.count_active_lockdown_bans(GUILD, self.now), 1)
        self.assertEqual(await self.db.count_lockdown_bans_since(GUILD, None), 1)
        self.assertIn("Lockdown: joiner banned", self.bot.logged)

        # Closed DMs still get banned.
        closed = _Member(self.guild, 2, dm_ok=False)
        self.assertTrue(await self.cog.handle_join(closed, self.now))
        self.assertEqual(len(self.guild.bans), 2)

        # Bots and other guilds are ignored.
        self.assertFalse(await self.cog.handle_join(_Member(self.guild, 3, bot=True), self.now))
        other = _Guild()
        other.id = 999
        self.assertFalse(await self.cog.handle_join(_Member(other, 4), self.now))
        self.assertEqual(len(self.guild.bans), 2)

    async def test_lockdown_turns_itself_off_after_the_timer(self) -> None:
        await self.db.set_lockdown_until(GUILD, self.now + timedelta(minutes=10))
        self.assertTrue(await self.cog.is_active(GUILD, self.now))
        await self.cog.sweep(self.now + timedelta(minutes=9))
        self.assertTrue(await self.cog.is_active(GUILD, self.now + timedelta(minutes=9)))
        await self.cog.sweep(self.now + timedelta(minutes=10))
        self.assertFalse(await self.cog.is_active(GUILD, self.now + timedelta(minutes=10)))
        self.assertIn("Lockdown ended", self.bot.logged)
        # A joiner after expiry is let in.
        self.assertFalse(await self.cog.handle_join(_Member(self.guild, 5), self.now + timedelta(minutes=11)))

    async def test_lockdown_bans_are_lifted_after_seven_days(self) -> None:
        await self.db.set_lockdown_until(GUILD, self.now + timedelta(minutes=5))
        await self.cog.handle_join(_Member(self.guild, 1), self.now)
        await self.cog.sweep(self.now + timedelta(days=LOCKDOWN_BAN_DAYS) - timedelta(minutes=1))
        self.assertEqual(self.guild.unbans, [])
        await self.cog.sweep(self.now + timedelta(days=LOCKDOWN_BAN_DAYS))
        self.assertEqual(self.guild.unbans, [1])
        later = self.now + timedelta(days=LOCKDOWN_BAN_DAYS)
        self.assertEqual(await self.db.count_active_lockdown_bans(GUILD, later), 0)
        self.assertEqual(await self.db.count_lockdown_bans_since(GUILD, None), 1)
        # Not unbanned twice.
        await self.cog.sweep(later + timedelta(hours=1))
        self.assertEqual(self.guild.unbans, [1])

    async def test_starting_lockdown_bans_members_who_joined_in_the_last_ten_minutes(self) -> None:
        recent = _Member(self.guild, 1)
        recent.joined_at = self.now - timedelta(minutes=3)
        edge = _Member(self.guild, 2)
        edge.joined_at = self.now - timedelta(minutes=10)
        old = _Member(self.guild, 3)
        old.joined_at = self.now - timedelta(minutes=11)
        staff = _Member(self.guild, 42)
        staff.joined_at = self.now - timedelta(minutes=1)
        bot_user = _Member(self.guild, 5, bot=True)
        bot_user.joined_at = self.now - timedelta(minutes=1)
        unknown = _Member(self.guild, 6)  # joined_at unavailable
        self.guild.members = [recent, edge, old, staff, bot_user, unknown]

        banned = await self.cog.ban_recent_joiners(self.guild, self.now)
        self.assertEqual(banned, 2)
        self.assertEqual(sorted(user_id for user_id, _ in self.guild.bans), [1, 2])
        self.assertEqual(recent.dms, [LOCKDOWN_DM])
        self.assertEqual(await self.db.count_active_lockdown_bans(GUILD, self.now), 2)

    async def test_timer_default_and_persistence_in_seconds(self) -> None:
        _, seconds = await self.cog._state(GUILD)
        self.assertEqual(seconds, 30 * 60)
        await self.db.set_lockdown_seconds(GUILD, 90)
        _, seconds = await self.cog._state(GUILD)
        self.assertEqual(seconds, 90)
        await self.db.set_lockdown_seconds(GUILD, 2 * 24 * 3600)
        _, seconds = await self.cog._state(GUILD)
        self.assertEqual(seconds, 172800)

    async def test_minutes_column_from_previous_version_is_migrated(self) -> None:
        import sqlite3
        path = Path(self.temp.name) / "old.db"
        con = sqlite3.connect(path)
        con.executescript(
            """
            CREATE TABLE guild_settings (
                guild_id INTEGER PRIMARY KEY,
                mod_log_channel_id INTEGER,
                dm_warnings INTEGER NOT NULL DEFAULT 1,
                dm_timeouts INTEGER NOT NULL DEFAULT 1,
                dm_bans INTEGER NOT NULL DEFAULT 1,
                timeout_cleanup_minutes INTEGER NOT NULL DEFAULT 0,
                lockdown_until TEXT,
                lockdown_minutes INTEGER NOT NULL DEFAULT 30
            );
            INSERT INTO guild_settings(guild_id, lockdown_minutes) VALUES (123, 45);
            """
        )
        con.commit()
        con.close()
        old = Database(path)
        await old.connect()
        try:
            settings = await old.get_guild_settings(GUILD)
            self.assertEqual(settings["lockdown_seconds"], 45 * 60)
        finally:
            await old.close()

    def test_group_exposes_toggle_cancel_timer_status(self) -> None:
        names = sorted(command.name for command in LockdownCog.__cog_app_commands__)
        self.assertEqual(names, ["cancel", "status", "timer", "toggle"])
        self.assertEqual(LockdownCog.__cog_group_name__, "lockdown")


if __name__ == "__main__":
    unittest.main()
