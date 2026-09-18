from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot import OctoTracker
from octotracker.announcements import parse_announcement_listing, parse_news_feed
from octotracker.config import Config
from octotracker.database import Database
from octotracker.status import (
    OFFLINE,
    ONLINE,
    PARTIAL,
    REALMS,
    UNKNOWN,
    StatusResult,
    parse_realm_statuses,
)


class ParserTests(unittest.TestCase):
    def test_status_parser_extracts_each_realm(self) -> None:
        html = """
        <section><h3>C'Thun (Hardcore)</h3><span>Online</span></section>
        <section><h3>N'Zoth (Normal)</h3><span>Offline</span></section>
        <section><h3>Y'Shaarj (PvP)</h3><span>Online</span></section>
        """
        self.assertEqual(
            parse_realm_statuses(html),
            {
                "C'Thun (Hardcore)": ONLINE,
                "N'Zoth (Normal)": OFFLINE,
                "Y'Shaarj (PvP)": ONLINE,
            },
        )

    def test_status_parser_handles_live_realm_row_markup(self) -> None:
        html = """
        <div class="realm-row">
          <div class="realm-name">C'Thun <span>(Hardcore)</span><span>?</span></div>
          <div class="realm-status online">Online</div>
        </div>
        <div class="realm-row">
          <div class="realm-name">N'Zoth <span>(Normal)</span><span>?</span></div>
          <div class="realm-status offline">Offline</div>
        </div>
        <div class="realm-row">
          <div class="realm-name">Y'Shaarj <span>(PvP)</span><span>?</span></div>
          <div class="realm-status online">Online</div>
        </div>
        """
        parsed = parse_realm_statuses(html)
        self.assertEqual(
            list(parsed.values()), [ONLINE, OFFLINE, ONLINE]
        )

    def test_missing_status_is_unknown(self) -> None:
        parsed = parse_realm_statuses("<p>C'Thun (Hardcore)</p>")
        self.assertEqual(parsed["C'Thun (Hardcore)"], UNKNOWN)

    def test_announcement_parser_uses_topic_id_not_last_post(self) -> None:
        html = """
        <ul>
          <li class="row">
            <div class="list-inner">
              <a class="topictitle" href="./viewtopic.php?f=2&amp;t=4321">
                Maintenance notice
              </a>
              by <a class="username">Kestrel</a>
              <time datetime="2026-09-09T10:30:00+00:00">Today</time>
            </div>
            <a href="./viewtopic.php?p=999#p999">Latest reply</a>
          </li>
        </ul>
        """
        topics = parse_announcement_listing(html)
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0].topic_id, 4321)
        self.assertEqual(topics[0].author, "Kestrel")
        self.assertIn("t=4321", topics[0].url)

    def test_official_news_feed_keeps_phpbb_topic_id(self) -> None:
        topics = parse_news_feed(
            {
                "items": [
                    {
                        "id": "stable-feed-id",
                        "title": "A new topic",
                        "date": "2026-09-09T10:30:00+00:00",
                        "body": "A useful preview",
                        "author": "Kestrel",
                        "url": "https://octowow.st/forum/viewtopic.php?t=9876",
                    }
                ]
            }
        )
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0].topic_id, 9876)
        self.assertEqual(topics[0].preview, "A useful preview")


class DatabaseStateMachineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "test.db"
        self.database = Database(self.database_path)
        await self.database.connect()
        self.now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temp_dir.cleanup()

    def result(
        self, offset: int, cthun: str, nzoth: str = ONLINE, yshaarj: str = ONLINE
    ) -> StatusResult:
        return StatusResult(
            checked_at=self.now + timedelta(seconds=offset),
            realms=dict(zip(REALMS, (cthun, nzoth, yshaarj))),
            reachable=cthun != UNKNOWN,
            error="request failed" if cthun == UNKNOWN else None,
        )

    async def test_failures_do_not_create_outages_and_changes_need_three_checks(self) -> None:
        snapshot, transition = await self.database.apply_status_result(
            self.result(0, ONLINE), 3
        )
        self.assertEqual(snapshot.overall_status, ONLINE)
        self.assertIsNotNone(transition)

        snapshot, transition = await self.database.apply_status_result(
            self.result(30, UNKNOWN, UNKNOWN, UNKNOWN), 3
        )
        self.assertEqual(snapshot.overall_status, ONLINE)
        self.assertIsNone(transition)
        self.assertFalse(snapshot.source_ok)

        for offset in (60, 90):
            snapshot, transition = await self.database.apply_status_result(
                self.result(offset, OFFLINE), 3
            )
            self.assertEqual(snapshot.overall_status, ONLINE)
            self.assertIsNone(transition)

        snapshot, transition = await self.database.apply_status_result(
            self.result(120, OFFLINE), 3
        )
        self.assertEqual(snapshot.overall_status, PARTIAL)
        self.assertEqual(transition.kind, "outage")

        for offset in (150, 180):
            snapshot, transition = await self.database.apply_status_result(
                self.result(offset, ONLINE), 3
            )
            self.assertEqual(snapshot.overall_status, PARTIAL)
            self.assertIsNone(transition)

        snapshot, transition = await self.database.apply_status_result(
            self.result(210, ONLINE), 3
        )
        self.assertEqual(snapshot.overall_status, ONLINE)
        self.assertEqual(transition.kind, "recovery")
        self.assertEqual(transition.duration_seconds, 90)

        incidents = await self.database.recent_incidents()
        self.assertEqual(len(incidents), 1)
        self.assertIsNotNone(incidents[0].ended_at)
        self.assertEqual(
            [item.kind for item in await self.database.pending_incident_alerts()],
            ["outage", "recovery"],
        )

    async def test_confirmed_state_survives_database_restart(self) -> None:
        await self.database.apply_status_result(self.result(0, ONLINE), 3)
        await self.database.close()

        self.database = Database(self.database_path)
        await self.database.connect()
        snapshot = await self.database.get_status_snapshot()
        self.assertEqual(snapshot.overall_status, ONLINE)
        self.assertTrue(all(realm.status == ONLINE for realm in snapshot.realms))

    async def test_partial_to_total_outage_remains_one_incident(self) -> None:
        await self.database.apply_status_result(self.result(0, ONLINE), 3)
        for offset in (30, 60, 90):
            snapshot, _ = await self.database.apply_status_result(
                self.result(offset, OFFLINE), 3
            )
        interruption_started = snapshot.status_since
        self.assertEqual(snapshot.overall_status, PARTIAL)

        for offset in (120, 150, 180):
            snapshot, transition = await self.database.apply_status_result(
                self.result(offset, OFFLINE, OFFLINE, OFFLINE), 3
            )
        self.assertEqual(snapshot.overall_status, OFFLINE)
        self.assertEqual(snapshot.status_since, interruption_started)
        self.assertIsNone(transition.kind)
        self.assertEqual(len(await self.database.recent_incidents()), 1)


class CommandRegistrationTests(unittest.TestCase):
    def test_commands_exist_only_in_configured_guild(self) -> None:
        config = Config(
            token="test-token",
            guild_id=123456789,
            status_channel_id=None,
            alert_channel_id=None,
            announcement_channel_id=None,
        )
        bot = OctoTracker(config)
        guild_commands = bot.tree.get_commands(guild=bot.guild)
        self.assertEqual(
            sorted(command.name for command in guild_commands),
            [
                "announcement",
                "authcheck",
                "config",
                "help",
                "incidents",
                "nextshows",
                "radio",
                "report",
                "reports",
                "reports-clear",
                "reports-details",
                "status",
                "uptime",
            ],
        )
        self.assertEqual(bot.tree.get_commands(), [])
        self.assertEqual(
            sum(command.name == "status" for command in guild_commands), 1
        )
        authcheck = next(
            command for command in guild_commands if command.name == "authcheck"
        )
        # Public command, restricted by channel at runtime rather than by permission.
        self.assertIsNone(authcheck.default_permissions)
        config_group = next(
            command for command in guild_commands if command.name == "config"
        )
        command_role_group = next(
            command
            for command in config_group.commands
            if command.name == "command-role"
        )
        self.assertEqual(
            sorted(command.name for command in command_role_group.commands),
            ["add", "list", "remove"],
        )


if __name__ == "__main__":
    unittest.main()
