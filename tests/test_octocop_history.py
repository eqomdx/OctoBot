from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from octocop.database import Database


class ModerationHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_untimeout_removes_timeout_from_check_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "octobot.db")
            await database.connect()
            try:
                started = datetime.now(timezone.utc)
                timeout_id = await database.add_timeout(
                    guild_id=123,
                    user_id=456,
                    moderator_id=789,
                    duration_seconds=3600,
                    reason="Test timeout",
                    started_at=started,
                    expires_at=started + timedelta(hours=1),
                )

                before = await database.get_moderation_summary(123, 456)
                self.assertEqual(before["timeout_count"], 1)
                self.assertEqual(before["total_timeout_seconds"], 3600)
                self.assertEqual(before["last_timeout"]["id"], timeout_id)

                ended_id = await database.end_latest_timeout(
                    guild_id=123,
                    user_id=456,
                    moderator_id=999,
                    reason="Removed early",
                )
                self.assertEqual(ended_id, timeout_id)

                after = await database.get_moderation_summary(123, 456)
                self.assertEqual(after["timeout_count"], 0)
                self.assertEqual(after["total_timeout_seconds"], 0)
                self.assertIsNone(after["last_timeout"])

                async with database.db.execute(
                    "SELECT ended_at, ended_by, end_reason, excluded_from_history FROM timeouts WHERE id = ?",
                    (timeout_id,),
                ) as cur:
                    row = await cur.fetchone()
                self.assertIsNotNone(row["ended_at"])
                self.assertEqual(row["ended_by"], 999)
                self.assertEqual(row["end_reason"], "Removed early")
                self.assertEqual(row["excluded_from_history"], 1)
            finally:
                await database.close()


    async def test_individual_history_entry_removal_updates_summary_and_lists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "octobot.db")
            await database.connect()
            try:
                warning_id = await database.add_warning(123, 456, 789, "Warning reason")
                started = datetime.now(timezone.utc)
                timeout_id = await database.add_timeout(
                    123, 456, 789, 90, "Timeout reason", started, started + timedelta(seconds=90)
                )

                history = await database.list_moderation_history(123, 456)
                self.assertEqual({row["kind"] for row in history}, {"warning", "timeout"})

                removed = await database.exclude_history_entry(123, 456, "warning", warning_id)
                self.assertIsNotNone(removed)
                summary = await database.get_moderation_summary(123, 456)
                self.assertEqual(summary["warning_count"], 0)
                self.assertEqual(summary["timeout_count"], 1)
                self.assertEqual(summary["total_timeout_seconds"], 90)
                self.assertEqual(await database.list_warnings(123, 456), [])
                history = await database.list_moderation_history(123, 456)
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0]["kind"], "timeout")
                self.assertEqual(history[0]["id"], timeout_id)
            finally:
                await database.close()

    async def test_clear_history_excludes_all_cases_without_deleting_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "octobot.db")
            await database.connect()
            try:
                await database.add_warning(123, 456, 789, "Warning one")
                await database.add_warning(123, 456, 789, "Warning two")
                started = datetime.now(timezone.utc)
                await database.add_timeout(
                    123, 456, 789, 3600, "Timeout one", started, started + timedelta(hours=1)
                )

                result = await database.clear_moderation_history(123, 456)
                self.assertEqual(result, {"warnings": 2, "timeouts": 1})
                summary = await database.get_moderation_summary(123, 456)
                self.assertEqual(summary["warning_count"], 0)
                self.assertEqual(summary["timeout_count"], 0)
                self.assertEqual(summary["total_timeout_seconds"], 0)
                self.assertEqual(await database.list_moderation_history(123, 456), [])

                async with database.db.execute(
                    "SELECT COUNT(*) AS count FROM warnings WHERE guild_id = 123 AND user_id = 456"
                ) as cur:
                    self.assertEqual(int((await cur.fetchone())["count"]), 2)
                async with database.db.execute(
                    "SELECT COUNT(*) AS count FROM timeouts WHERE guild_id = 123 AND user_id = 456"
                ) as cur:
                    self.assertEqual(int((await cur.fetchone())["count"]), 1)
            finally:
                await database.close()

    async def test_migration_adds_history_exclusion_column(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "octobot.db"
            database = Database(path)
            await database.connect()
            await database.close()

            # Simulate an older database by rebuilding timeouts without the new column.
            import sqlite3
            con = sqlite3.connect(path)
            con.executescript(
                """
                ALTER TABLE timeouts RENAME TO timeouts_newer;
                CREATE TABLE timeouts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    cleanup_minutes INTEGER NOT NULL DEFAULT 0,
                    messages_deleted INTEGER NOT NULL DEFAULT 0,
                    ended_at TEXT,
                    ended_by INTEGER,
                    end_reason TEXT
                );
                DROP TABLE timeouts_newer;
                """
            )
            con.commit()
            con.close()

            database = Database(path)
            await database.connect()
            try:
                async with database.db.execute("PRAGMA table_info(timeouts)") as cur:
                    columns = {row["name"] for row in await cur.fetchall()}
                self.assertIn("excluded_from_history", columns)
            finally:
                await database.close()


if __name__ == "__main__":
    unittest.main()
