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
                self.assertEqual(result, {"warnings": 2, "timeouts": 1, "bans": 0})
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

    async def test_ban_stays_in_check_history_after_unban(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "octobot.db")
            await database.connect()
            try:
                ban_id = await database.add_ban(123, 456, 789, "Ban reason")

                summary = await database.get_moderation_summary(123, 456)
                self.assertEqual(summary["ban_count"], 1)
                history = await database.list_moderation_history(123, 456)
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0]["kind"], "ban")
                self.assertEqual(history[0]["id"], ban_id)
                self.assertEqual(history[0]["reason"], "Ban reason")
                self.assertIsNone(history[0]["unbanned_at"])

                # An unban closes the case but, unlike /untimeout, keeps it visible.
                ended_id = await database.mark_latest_ban_unbanned(123, 456, None, None)
                self.assertEqual(ended_id, ban_id)
                self.assertIsNone(await database.mark_latest_ban_unbanned(123, 456, None, None))

                summary = await database.get_moderation_summary(123, 456)
                self.assertEqual(summary["ban_count"], 1)
                history = await database.list_moderation_history(123, 456)
                self.assertEqual(len(history), 1)
                self.assertIsNotNone(history[0]["unbanned_at"])

                # Manual removal and full clears cover bans like any other case.
                removed = await database.exclude_history_entry(123, 456, "ban", ban_id)
                self.assertIsNotNone(removed)
                self.assertEqual(removed["kind"], "ban")
                self.assertEqual((await database.get_moderation_summary(123, 456))["ban_count"], 0)
                self.assertEqual(await database.list_moderation_history(123, 456), [])

                await database.add_ban(123, 456, 789, "Second ban")
                await database.add_warning(123, 456, 789, "Warning")
                result = await database.clear_moderation_history(123, 456)
                self.assertEqual(result, {"warnings": 1, "timeouts": 0, "bans": 1})
                async with database.db.execute(
                    "SELECT COUNT(*) AS count FROM bans WHERE guild_id = 123 AND user_id = 456"
                ) as cur:
                    self.assertEqual(int((await cur.fetchone())["count"]), 2)
            finally:
                await database.close()

    async def test_migration_adds_ban_permission_to_full_access_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "octobot.db"
            database = Database(path)
            await database.connect()
            await database.close()

            # Simulate a pre-ban database: no can_ban column, no dm_bans column, no bans table.
            import sqlite3
            con = sqlite3.connect(path)
            con.executescript(
                """
                DROP TABLE bans;
                ALTER TABLE role_permissions RENAME TO role_permissions_newer;
                CREATE TABLE role_permissions (
                    guild_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    can_timeout INTEGER NOT NULL DEFAULT 0,
                    can_warn INTEGER NOT NULL DEFAULT 0,
                    can_view_warnings INTEGER NOT NULL DEFAULT 0,
                    can_check INTEGER NOT NULL DEFAULT 0,
                    can_untimeout INTEGER NOT NULL DEFAULT 0,
                    can_manage_settings INTEGER NOT NULL DEFAULT 0,
                    max_timeout_seconds INTEGER NOT NULL DEFAULT 2419200,
                    PRIMARY KEY (guild_id, role_id)
                );
                DROP TABLE role_permissions_newer;
                INSERT INTO role_permissions(guild_id, role_id, can_timeout, can_untimeout, max_timeout_seconds)
                    VALUES (1, 10, 1, 1, 3600);
                INSERT INTO role_permissions(guild_id, role_id, can_timeout, can_warn, can_view_warnings,
                    can_check, can_untimeout, can_manage_settings)
                    VALUES (1, 20, 1, 1, 1, 1, 1, 1);
                ALTER TABLE guild_settings RENAME TO guild_settings_newer;
                CREATE TABLE guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    mod_log_channel_id INTEGER,
                    dm_warnings INTEGER NOT NULL DEFAULT 1,
                    dm_timeouts INTEGER NOT NULL DEFAULT 1,
                    timeout_cleanup_minutes INTEGER NOT NULL DEFAULT 0
                );
                DROP TABLE guild_settings_newer;
                """
            )
            con.commit()
            con.close()

            database = Database(path)
            await database.connect()
            try:
                helper = await database.get_role_profile(1, 10)
                moderator = await database.get_role_profile(1, 20)
                self.assertFalse(helper.can_ban)
                self.assertTrue(moderator.can_ban)
                settings = await database.get_guild_settings(1)
                self.assertEqual(settings["dm_bans"], 1)
                self.assertEqual(await database.add_ban(1, 2, 3, "works"), 1)
            finally:
                await database.close()


if __name__ == "__main__":
    unittest.main()
