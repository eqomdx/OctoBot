from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from octotracker.auth import (
    MALFORMED_RESPONSE,
    RESPONSIVE,
    UNKNOWN as AUTH_UNKNOWN,
    UNRESPONSIVE,
    AuthState,
)
from octotracker.connectivity import (
    REACHABLE,
    UNKNOWN as CONNECTION_UNKNOWN,
    UNREACHABLE,
    ConnectivityState,
)
from octotracker.database import Database
from octotracker.permissions import (
    command_access_allowed,
    command_role_label,
)
from octotracker.reports import ReportSummary
from octotracker.status import (
    DEGRADED,
    OFFLINE,
    ONLINE,
    PARTIAL,
    REALMS,
    UNKNOWN,
    CombinedStatusSnapshot,
    RealmState,
    StatusSnapshot,
    combine_status,
)


class ReportDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "reports.db")
        await self.database.connect()
        self.now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temp_dir.cleanup()

    async def test_duplicate_refresh_expiry_grouping_and_clear(self) -> None:
        first, refreshed = await self.database.submit_report(
            10, 100, "C'Thun", "High latency", "Initial", 10, now=self.now
        )
        self.assertFalse(refreshed)
        second, refreshed = await self.database.submit_report(
            10,
            100,
            "C'Thun",
            "High latency",
            "Updated",
            10,
            now=self.now + timedelta(minutes=5),
        )
        self.assertTrue(refreshed)
        self.assertEqual(first.report_id, second.report_id)
        self.assertEqual(second.details, "Updated")
        self.assertEqual(second.created_at, first.created_at)

        await self.database.submit_report(
            10,
            101,
            "C'Thun",
            "High latency",
            None,
            10,
            now=self.now + timedelta(minutes=6),
        )
        await self.database.submit_report(
            10,
            100,
            "All Realms",
            "Disconnecting",
            None,
            10,
            now=self.now + timedelta(minutes=7),
        )
        summary = await self.database.report_summary(
            10, 10, 3, now=self.now + timedelta(minutes=7)
        )
        self.assertEqual(summary.total_reports, 3)
        self.assertEqual(summary.unique_users, 2)
        self.assertEqual(len(summary.groups), 2)
        self.assertFalse(summary.degraded)

        expired, refreshed = await self.database.submit_report(
            10,
            100,
            "C'Thun",
            "High latency",
            None,
            10,
            now=self.now + timedelta(minutes=16),
        )
        self.assertFalse(refreshed)
        self.assertNotEqual(expired.report_id, first.report_id)

        cleared = await self.database.clear_reports(
            10, 999, 10, now=self.now + timedelta(minutes=16)
        )
        self.assertEqual(cleared, 3)
        summary = await self.database.report_summary(
            10, 10, 3, now=self.now + timedelta(minutes=16)
        )
        self.assertEqual(summary.total_reports, 0)

    async def test_three_unique_users_degrade_but_never_make_offline(self) -> None:
        for user_id in (1, 2, 3):
            await self.database.submit_report(
                20,
                user_id,
                "N'Zoth",
                "Can't connect",
                None,
                10,
                now=self.now,
            )
        reports = await self.database.report_summary(20, 10, 3, now=self.now)
        official = StatusSnapshot(
            overall_status=ONLINE,
            status_since=self.now,
            realms=tuple(
                RealmState(realm, ONLINE, self.now, None, 0, ONLINE)
                for realm in REALMS
            ),
            checked_at=self.now,
            last_success_at=self.now,
            source_ok=True,
            source_error=None,
        )
        connectivity = ConnectivityState(
            REACHABLE, self.now, None, 0, self.now, self.now, None, "host", 1
        )
        combined: CombinedStatusSnapshot = combine_status(
            official, connectivity, reports
        )
        self.assertEqual(combined.overall_status, DEGRADED)


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "roles.db")
        await self.database.connect()

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temp_dir.cleanup()

    def test_open_role_match_admin_and_deleted_role_label(self) -> None:
        self.assertTrue(command_access_allowed(set(), set(), administrator=False))
        self.assertTrue(
            command_access_allowed({11}, {11, 12}, administrator=False)
        )
        self.assertFalse(command_access_allowed({11}, {12}, administrator=False))
        self.assertTrue(command_access_allowed({11}, set(), administrator=True))
        self.assertEqual(command_role_label(11, set()), "Deleted role (ID 11)")
        self.assertEqual(command_role_label(11, {11}), "<@&11>")

    async def test_command_roles_persist_and_remove(self) -> None:
        self.assertEqual(await self.database.command_roles(10, "status"), set())
        self.assertTrue(await self.database.add_command_role(10, "status", 22, 1))
        self.assertFalse(await self.database.add_command_role(10, "status", 22, 1))
        self.assertEqual(await self.database.command_roles(10, "status"), {22})
        self.assertTrue(await self.database.remove_command_role(10, "status", 22))
        self.assertEqual(await self.database.command_roles(10, "status"), set())


class CombinedStatusTests(unittest.TestCase):
    now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

    def official(self, status: str, *, source_ok: bool = True) -> StatusSnapshot:
        return StatusSnapshot(
            overall_status=status,
            status_since=self.now,
            realms=tuple(
                RealmState(realm, status, self.now, None, 0, status)
                for realm in REALMS
            ),
            checked_at=self.now,
            last_success_at=self.now,
            source_ok=source_ok,
            source_error=None if source_ok else "challenge",
        )

    def connection(self, status: str) -> ConnectivityState:
        return ConnectivityState(
            status, self.now, None, 0, self.now, self.now, None, "host", 1
        )

    def authentication(self, status: str) -> AuthState:
        return AuthState(
            status=status,
            status_since=self.now,
            pending_status=None,
            pending_count=0,
            observed_status=status,
            tcp_status=REACHABLE,
            checked_at=self.now,
            last_success_at=self.now if status == RESPONSIVE else None,
            last_failure_at=self.now if status != RESPONSIVE else None,
            latency_ms=10,
            consecutive_successes=1 if status == RESPONSIVE else 0,
            consecutive_failures=0 if status == RESPONSIVE else 1,
            reason=None,
            host="host",
            port=3724,
        )

    @staticmethod
    def reports(users: int, threshold: int = 3) -> ReportSummary:
        return ReportSummary((), users, users, 10, threshold)

    def test_overall_precedence_and_insufficient_evidence(self) -> None:
        cases = (
            (OFFLINE, REACHABLE, 5, True, OFFLINE),
            (PARTIAL, REACHABLE, 5, True, PARTIAL),
            (ONLINE, UNREACHABLE, 0, True, DEGRADED),
            (ONLINE, REACHABLE, 3, True, DEGRADED),
            (ONLINE, CONNECTION_UNKNOWN, 0, True, ONLINE),
            (ONLINE, REACHABLE, 0, False, UNKNOWN),
            (ONLINE, REACHABLE, 0, True, ONLINE),
            (UNKNOWN, REACHABLE, 3, False, DEGRADED),
        )
        for official, connection, users, source_ok, expected in cases:
            with self.subTest(expected=expected):
                combined = combine_status(
                    self.official(official, source_ok=source_ok),
                    self.connection(connection),
                    self.reports(users),
                )
                self.assertEqual(combined.overall_status, expected)

    def test_confirmed_auth_failure_degrades_but_unknown_adds_no_conclusion(self) -> None:
        for auth_status, expected in (
            (RESPONSIVE, ONLINE),
            (AUTH_UNKNOWN, ONLINE),
            (UNRESPONSIVE, DEGRADED),
            (MALFORMED_RESPONSE, DEGRADED),
        ):
            with self.subTest(auth_status=auth_status):
                combined = combine_status(
                    self.official(ONLINE),
                    self.connection(REACHABLE),
                    self.reports(0),
                    self.authentication(auth_status),
                )
                self.assertEqual(combined.overall_status, expected)

        offline = combine_status(
            self.official(OFFLINE),
            self.connection(REACHABLE),
            self.reports(0),
            self.authentication(UNRESPONSIVE),
        )
        self.assertEqual(offline.overall_status, OFFLINE)


if __name__ == "__main__":
    unittest.main()
