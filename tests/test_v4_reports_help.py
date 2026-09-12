from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bot import HELP_DESCRIPTIONS, OctoTracker
from octotracker.auth import RESPONSIVE, AuthState
from octotracker.config import Config
from octotracker.connectivity import REACHABLE, ConnectivityState
from octotracker.database import Database
from octotracker.embeds import help_embed, report_degraded_embed, reports_embed, status_embed
from octotracker.permissions import MANAGED_COMMANDS
from octotracker.reports import ReportGroup, ReportSummary
from octotracker.status import ONLINE, REALMS, CombinedStatusSnapshot, RealmState, StatusSnapshot


class ReportAlertTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "reports-v4.db")
        await self.db.connect()
        self.guild_id = 123
        self.now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp_dir.cleanup()

    async def test_threshold_alert_is_deduplicated_and_recovery_is_once(self) -> None:
        degraded = ReportSummary((), 3, 3, 10, 3)
        await self.db.apply_report_alert_state(self.guild_id, degraded, now=self.now)
        pending = await self.db.pending_report_incident_alerts(self.guild_id)
        self.assertEqual([item.kind for item in pending], ["degraded"])
        incident_id = pending[0].incident.incident_id
        await self.db.mark_report_incident_alert_sent(incident_id, "degraded")

        await self.db.apply_report_alert_state(self.guild_id, degraded, now=self.now)
        self.assertEqual(await self.db.pending_report_incident_alerts(self.guild_id), [])

        healthy = ReportSummary((), 0, 0, 10, 3)
        await self.db.apply_report_alert_state(self.guild_id, healthy, now=self.now)
        self.assertEqual(await self.db.pending_report_incident_alerts(self.guild_id), [])
        await self.db.apply_report_alert_state(self.guild_id, healthy, now=self.now)
        pending = await self.db.pending_report_incident_alerts(self.guild_id)
        self.assertEqual([item.kind for item in pending], ["recovery"])
        await self.db.mark_report_incident_alert_sent(incident_id, "recovery")
        self.assertEqual(await self.db.pending_report_incident_alerts(self.guild_id), [])

    async def test_active_report_detail_query(self) -> None:
        await self.db.submit_report(
            self.guild_id, 55, "N'Zoth", "Can't connect", "Authenticating forever", 10, now=self.now
        )
        reports = await self.db.active_reports(self.guild_id, 10, now=self.now)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].details, "Authenticating forever")


class V4EmbedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        self.official = StatusSnapshot(
            overall_status=ONLINE,
            status_since=self.now,
            realms=tuple(
                RealmState(realm, ONLINE, self.now, None, 0, ONLINE) for realm in REALMS
            ),
            checked_at=self.now,
            last_success_at=self.now,
            source_ok=True,
            source_error=None,
        )
        self.connection = ConnectivityState(
            REACHABLE, self.now, None, 0, self.now, self.now, None, "play.octowow.st", 3724
        )
        self.auth = AuthState(
            status=RESPONSIVE,
            status_since=self.now,
            pending_status=None,
            pending_count=0,
            observed_status=RESPONSIVE,
            tcp_status=REACHABLE,
            checked_at=self.now,
            last_success_at=self.now,
            last_failure_at=None,
            latency_ms=77,
            consecutive_successes=3,
            consecutive_failures=0,
            reason="Valid logon challenge response (result 4)",
            host="play.octowow.st",
            port=3724,
        )

    def test_no_reports_are_explicitly_visible(self) -> None:
        reports = ReportSummary((), 0, 0, 10, 3)
        snapshot = CombinedStatusSnapshot(
            self.official, self.connection, reports, ONLINE, self.auth
        )
        embed = status_embed(snapshot, [], None)
        field = next(field for field in embed.fields if field.name == "Community Reports")
        self.assertIn("No recent connection reports", field.value)

    def test_reports_embed_and_alert_show_counts(self) -> None:
        reports = ReportSummary(
            (ReportGroup("N'Zoth", "Can't connect", 3, 3, self.now),),
            3,
            3,
            10,
            3,
        )
        embed = reports_embed(reports)
        self.assertIn("3 unique", embed.description)

        snapshot = CombinedStatusSnapshot(
            self.official, self.connection, reports, "degraded", self.auth
        )
        from octotracker.database import ReportIncident

        incident = ReportIncident(1, 123, self.now, None, 3, 3)
        alert = report_degraded_embed(incident, reports, snapshot)
        self.assertIn("3 users", alert.description)

    def test_help_embed_mentions_command_channel(self) -> None:
        embed = help_embed(
            [("status", HELP_DESCRIPTIONS["status"]), ("radio", HELP_DESCRIPTIONS["radio"])],
            "<#999>",
        )
        self.assertIn("<#999>", embed.description)
        self.assertEqual([field.name for field in embed.fields], ["/status", "/radio"])


class V4RegistrationTests(unittest.TestCase):
    def test_radio_and_help_are_registered_and_radio_is_role_managed(self) -> None:
        config = Config(
            token="test-token",
            guild_id=123456789,
            status_channel_id=None,
            alert_channel_id=None,
            announcement_channel_id=None,
        )
        bot = OctoTracker(config)
        names = [command.name for command in bot.tree.get_commands(guild=bot.guild)]
        self.assertEqual(names.count("help"), 1)
        self.assertEqual(names.count("radio"), 1)
        self.assertIn("radio", MANAGED_COMMANDS)
        self.assertNotIn("help", MANAGED_COMMANDS)


if __name__ == "__main__":
    unittest.main()
