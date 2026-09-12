from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from octotracker.database import Database


V1_SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    initial_status TEXT NOT NULL,
    worst_status TEXT NOT NULL
);
CREATE TABLE announcements (
    topic_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT,
    published_at TEXT,
    preview TEXT,
    url TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    announced INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE service_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
"""


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_v1_data_and_message_id_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "v1.db"
            connection = sqlite3.connect(path)
            connection.executescript(V1_SCHEMA)
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                ("status_message_id:123", "456"),
            )
            connection.execute(
                """
                INSERT INTO incidents(started_at, initial_status, worst_status)
                VALUES ('2026-01-01T00:00:00+00:00', 'partial', 'offline')
                """
            )
            connection.execute(
                """
                INSERT INTO announcements(
                    topic_id, title, preview, url, discovered_at, announced
                ) VALUES (
                    99, 'Legacy topic', 'Legacy preview',
                    'https://octowow.st/forum/viewtopic.php?t=99',
                    '2026-01-01T00:00:00+00:00', 1
                )
                """
            )
            connection.execute(
                """
                INSERT INTO service_history(status, started_at)
                VALUES ('online', '2026-01-01T00:00:00+00:00')
                """
            )
            connection.commit()
            connection.close()

            database = Database(path)
            await database.connect()
            try:
                self.assertEqual(
                    await database.get_metadata("status_message_id:123"), "456"
                )
                self.assertEqual(await database.get_metadata("schema_version"), "4")
                latest = await database.latest_announcement()
                self.assertEqual(latest.topic_id, 99)
                self.assertEqual(latest.preview, "Legacy preview")

                cursor = await database.connection.execute(
                    "SELECT COUNT(*) AS count FROM service_history"
                )
                self.assertEqual((await cursor.fetchone())["count"], 1)
                cursor = await database.connection.execute(
                    "SELECT outage_alert_sent, recovery_alert_sent FROM incidents"
                )
                incident = await cursor.fetchone()
                self.assertEqual(incident["outage_alert_sent"], 0)
                self.assertEqual(incident["recovery_alert_sent"], 0)
                cursor = await database.connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table'
                      AND name IN (
                          'community_reports', 'command_roles',
                          'connectivity_state', 'connectivity_history',
                          'auth_state', 'auth_history', 'auth_incidents',
                          'radio_state', 'radio_show_history',
                          'report_alert_state', 'report_incidents'
                      )
                    """
                )
                self.assertEqual(len(await cursor.fetchall()), 11)
            finally:
                await database.close()

    async def test_v2_database_adds_auth_tables_without_losing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "v2.db"
            database = Database(path)
            await database.connect()
            await database.set_metadata("v2-marker", "preserve-me")
            await database.close()

            connection = sqlite3.connect(path)
            connection.executescript(
                """
                DROP TABLE auth_incidents;
                DROP TABLE auth_history;
                DROP TABLE auth_state;
                UPDATE metadata SET value = '2' WHERE key = 'schema_version';
                """
            )
            connection.commit()
            connection.close()

            await database.connect()
            try:
                self.assertEqual(await database.get_metadata("v2-marker"), "preserve-me")
                self.assertEqual(await database.get_metadata("schema_version"), "4")
                auth_state = await database.get_auth_state()
                self.assertEqual(auth_state.status, "unknown")
            finally:
                await database.close()

    async def test_v3_database_upgrades_to_v4_without_losing_existing_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "v3.db"
            database = Database(path)
            await database.connect()
            await database.set_metadata("v3-marker", "preserve-me")
            await database.close()

            # Approximate the V3 production shape by removing only V4 tables.
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                DROP TABLE radio_show_history;
                DROP TABLE radio_state;
                DROP TABLE report_incidents;
                DROP TABLE report_alert_state;
                UPDATE metadata SET value = '3' WHERE key = 'schema_version';
                """
            )
            connection.commit()
            connection.close()

            database = Database(path)
            await database.connect()
            try:
                self.assertEqual(await database.get_metadata("v3-marker"), "preserve-me")
                self.assertEqual(await database.get_metadata("schema_version"), "4")
                cursor = await database.connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table'
                      AND name IN (
                          'radio_state', 'radio_show_history',
                          'report_alert_state', 'report_incidents'
                      )
                    """
                )
                self.assertEqual(len(await cursor.fetchall()), 4)
            finally:
                await database.close()


if __name__ == "__main__":
    unittest.main()
