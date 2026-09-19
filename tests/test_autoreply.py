from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from octocop.cogs.autoreply import DEFAULT_COOLDOWN_SECONDS, AutoReplyCog
from octocop.database import Database

GUILD = 123


class _Message:
    def __init__(self, content: str, *, channel_id: int = 1, bot: bool = False, guild_id: int = GUILD):
        self.content = content
        self.guild = SimpleNamespace(id=guild_id)
        self.channel = SimpleNamespace(id=channel_id)
        self.author = SimpleNamespace(bot=bot)
        self.replies: list[str] = []

    async def reply(self, content, **kwargs):
        self.replies.append(content)


class AutoReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "octobot.db")
        await self.db.connect()
        self.bot = SimpleNamespace(moderation_database=self.db, config=SimpleNamespace(guild_id=GUILD))
        self.cog = AutoReplyCog(self.bot)
        await self.db.upsert_auto_reply(
            GUILD, "down", "Server is currently down for scheduled maintenance, read more here: link", 9
        )
        await self.db.upsert_auto_reply(GUILD, "realm list", "Set realmlist to play.octowow.st", 9)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp.cleanup()

    async def test_keyword_in_message_gets_the_fixed_reply(self) -> None:
        message = _Message("is the server DOWN again?")
        self.assertTrue(await self.cog.handle_message(message, now=0.0))
        self.assertEqual(message.replies, ["Server is currently down for scheduled maintenance, read more here: link"])

        phrase = _Message("what's the realm-list?", channel_id=2)
        self.assertTrue(await self.cog.handle_message(phrase, now=0.0))
        self.assertEqual(phrase.replies, ["Set realmlist to play.octowow.st"])

    async def test_no_reply_for_partial_words_bots_or_other_guilds(self) -> None:
        self.assertFalse(await self.cog.handle_message(_Message("download it"), now=0.0))
        self.assertFalse(await self.cog.handle_message(_Message("countdown"), now=0.0))
        self.assertFalse(await self.cog.handle_message(_Message("down", bot=True), now=0.0))
        self.assertFalse(await self.cog.handle_message(_Message("down", guild_id=999), now=0.0))
        self.assertFalse(await self.cog.handle_message(_Message("all fine"), now=0.0))

    async def test_cooldown_is_per_keyword_per_channel(self) -> None:
        self.assertTrue(await self.cog.handle_message(_Message("down"), now=0.0))
        self.assertFalse(await self.cog.handle_message(_Message("down?"), now=10.0))
        self.assertTrue(await self.cog.handle_message(_Message("down", channel_id=2), now=10.0))
        self.assertTrue(await self.cog.handle_message(_Message("realm list"), now=10.0))
        self.assertTrue(await self.cog.handle_message(_Message("down"), now=DEFAULT_COOLDOWN_SECONDS + 1.0))

    async def test_cooldown_is_configurable_and_persisted(self) -> None:
        await self.cog._ensure_loaded()
        self.assertEqual(self.cog.cooldown_seconds, 60)
        await self.db.set_autoreply_cooldown(GUILD, 5)
        fresh = AutoReplyCog(self.bot)
        await fresh._ensure_loaded()
        self.assertEqual(fresh.cooldown_seconds, 5)
        self.assertTrue(await fresh.handle_message(_Message("down"), now=0.0))
        self.assertFalse(await fresh.handle_message(_Message("down"), now=4.0))
        self.assertTrue(await fresh.handle_message(_Message("down"), now=5.0))

    async def test_database_round_trip_and_removal(self) -> None:
        rows = await self.db.list_auto_replies(GUILD)
        self.assertEqual([row["keyword"] for row in rows], ["down", "realm list"])
        await self.db.upsert_auto_reply(GUILD, "down", "Back up soon", 9)
        rows = {row["keyword"]: row["response"] for row in await self.db.list_auto_replies(GUILD)}
        self.assertEqual(rows["down"], "Back up soon")
        self.assertTrue(await self.db.remove_auto_reply(GUILD, "down"))
        self.assertFalse(await self.db.remove_auto_reply(GUILD, "down"))
        self.assertEqual([row["keyword"] for row in await self.db.list_auto_replies(GUILD)], ["realm list"])

    def test_group_exposes_add_edit_remove_list(self) -> None:
        names = sorted(command.name for command in AutoReplyCog.__cog_app_commands__)
        self.assertEqual(names, ["add", "cooldown", "edit", "list", "remove"])
        self.assertEqual(AutoReplyCog.__cog_group_name__, "autoreply")


if __name__ == "__main__":
    unittest.main()
