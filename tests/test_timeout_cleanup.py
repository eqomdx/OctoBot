from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import discord

from octocop.cogs.moderation import CLEANUP_PER_CHANNEL_LIMIT, ModerationCog
from octocop.database import Database

GUILD = 123


class _Perms:
    def __init__(self, **flags):
        self.view_channel = flags.get("view_channel", True)
        self.read_message_history = flags.get("read_message_history", True)
        self.manage_messages = flags.get("manage_messages", True)


class _Channel:
    def __init__(self, channel_id: int, *, deleted: int = 0, perms: _Perms | None = None, fail: bool = False):
        self.id = channel_id
        self._deleted = deleted
        self._perms = perms or _Perms()
        self._fail = fail
        self.calls: list[dict] = []

    def permissions_for(self, member):
        return self._perms

    async def purge(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail:
            raise discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "boom")
        return [object()] * self._deleted


class _Guild:
    def __init__(self, channels, threads=()):
        self.id = GUILD
        self.channels = channels
        self.threads = list(threads)
        self.me = SimpleNamespace(id=999)


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cog = ModerationCog(SimpleNamespace())
        self.user = SimpleNamespace(id=7)
        self.moderator = SimpleNamespace(id=8, __str__=lambda self: "Mod")

    async def test_purges_every_permitted_channel_and_counts_results(self) -> None:
        ok_one = _Channel(1, deleted=3)
        ok_two = _Channel(2, deleted=1)
        empty = _Channel(3, deleted=0)
        thread = _Channel(4, deleted=2)
        no_perms = _Channel(5, perms=_Perms(manage_messages=False))
        broken = _Channel(6, fail=True)
        guild = _Guild([ok_one, ok_two, empty, no_perms, broken], threads=[thread])

        deleted, scanned, skipped = await self.cog._cleanup_recent_messages(
            guild=guild, user=self.user, minutes=5, moderator=self.moderator
        )
        self.assertEqual(deleted, 6)
        self.assertEqual(scanned, 4)  # three readable channels plus the thread; the failing one counts as skipped
        self.assertEqual(skipped, 2)  # no permission, and the failing purge

    async def test_purge_is_bulk_bounded_and_filters_to_the_target_user(self) -> None:
        channel = _Channel(1, deleted=1)
        guild = _Guild([channel])
        before = datetime.now(timezone.utc) - timedelta(minutes=5)

        await self.cog._cleanup_recent_messages(
            guild=guild, user=self.user, minutes=5, moderator=self.moderator
        )
        call = channel.calls[0]
        self.assertTrue(call["bulk"])
        self.assertEqual(call["limit"], CLEANUP_PER_CHANNEL_LIMIT)
        self.assertGreaterEqual(call["after"], before)
        self.assertIn("timeout cleanup 5m", call["reason"])
        check = call["check"]
        self.assertTrue(check(SimpleNamespace(author=SimpleNamespace(id=7))))
        self.assertFalse(check(SimpleNamespace(author=SimpleNamespace(id=8))))

    async def test_disabled_cleanup_and_missing_bot_member_do_nothing(self) -> None:
        channel = _Channel(1, deleted=5)
        guild = _Guild([channel])
        self.assertEqual(
            await self.cog._cleanup_recent_messages(
                guild=guild, user=self.user, minutes=0, moderator=self.moderator
            ),
            (0, 0, 0),
        )
        guild.me = None
        self.assertEqual(
            await self.cog._cleanup_recent_messages(
                guild=guild, user=self.user, minutes=5, moderator=self.moderator
            ),
            (0, 0, 1),
        )
        self.assertEqual(channel.calls, [])


class ReplaceTimeoutPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_names_the_moderator_duration_and_reason(self) -> None:
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        user = SimpleNamespace(mention="<@7>")
        existing = {
            "id": 12,
            "moderator_id": 555,
            "duration_seconds": 3600,
            "reason": "Spamming links",
        }
        prompt = ModerationCog._replace_timeout_prompt(
            user=user, existing=existing, current_until=now + timedelta(minutes=20),
            seconds=600, reason="Still spamming", now=now,
        )
        self.assertIn("already timed out", prompt)
        self.assertIn("T-0012", prompt)
        self.assertIn("<@555>", prompt)
        self.assertIn("1h", prompt)
        self.assertIn("Spamming links", prompt)
        self.assertIn("20m** remains", prompt)
        self.assertIn("new **10m** timeout", prompt)
        self.assertIn("Still spamming", prompt)

    async def test_prompt_without_a_recorded_case_still_shows_the_remaining_time(self) -> None:
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        prompt = ModerationCog._replace_timeout_prompt(
            user=SimpleNamespace(mention="<@7>"), existing=None,
            current_until=now + timedelta(minutes=5), seconds=60, reason="New reason", now=now,
        )
        self.assertIn("5m** remains", prompt)
        self.assertNotIn("T-", prompt)


class LatestOpenTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_the_newest_unended_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "octobot.db")
            await database.connect()
            try:
                start = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)
                self.assertIsNone(await database.latest_open_timeout(GUILD, 7))
                first = await database.add_timeout(
                    GUILD, 7, 100, 600, "First", start, start + timedelta(minutes=10)
                )
                second = await database.add_timeout(
                    GUILD, 7, 200, 900, "Second", start + timedelta(minutes=1),
                    start + timedelta(minutes=16),
                )
                row = await database.latest_open_timeout(GUILD, 7)
                self.assertEqual(row["id"], second)
                self.assertEqual(row["moderator_id"], 200)

                await database.end_latest_timeout(GUILD, 7, 300, "replaced")
                row = await database.latest_open_timeout(GUILD, 7)
                self.assertEqual(row["id"], first)
            finally:
                await database.close()


if __name__ == "__main__":
    unittest.main()
