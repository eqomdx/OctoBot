from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from .duration import MAX_TIMEOUT_SECONDS


@dataclass(slots=True)
class RolePermissions:
    guild_id: int
    role_id: int
    can_timeout: bool = False
    can_warn: bool = False
    can_view_warnings: bool = False
    can_check: bool = False
    can_untimeout: bool = False
    can_manage_settings: bool = False
    max_timeout_seconds: int = MAX_TIMEOUT_SECONDS


class Database:
    PERMISSION_COLUMNS = {
        "timeout": "can_timeout",
        "warn": "can_warn",
        "warnings": "can_view_warnings",
        "check": "can_check",
        "untimeout": "can_untimeout",
        "settings": "can_manage_settings",
    }

    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self._init_schema()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self.conn is None:
            raise RuntimeError("Database is not connected")
        return self.conn

    async def _init_schema(self) -> None:
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                mod_log_channel_id INTEGER,
                dm_warnings INTEGER NOT NULL DEFAULT 1,
                dm_timeouts INTEGER NOT NULL DEFAULT 1,
                timeout_cleanup_minutes INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS role_permissions (
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

            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                excluded_from_history INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_warnings_guild_user
                ON warnings(guild_id, user_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS timeouts (
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
                end_reason TEXT,
                excluded_from_history INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_timeouts_guild_user
                ON timeouts(guild_id, user_id, started_at DESC);

            CREATE TABLE IF NOT EXISTS message_history (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                attachment_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                edited_at TEXT,
                deleted_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_message_history_guild_user
                ON message_history(guild_id, user_id, created_at DESC, message_id DESC);
            """
        )
        await self._migrate_schema()
        await self.db.commit()

    async def _migrate_schema(self) -> None:
        """Apply additive migrations so existing v1 databases upgrade in place."""
        async with self.db.execute("PRAGMA table_info(guild_settings)") as cur:
            guild_columns = {str(row["name"]) for row in await cur.fetchall()}
        if "timeout_cleanup_minutes" not in guild_columns:
            await self.db.execute(
                "ALTER TABLE guild_settings ADD COLUMN timeout_cleanup_minutes INTEGER NOT NULL DEFAULT 0"
            )

        async with self.db.execute("PRAGMA table_info(warnings)") as cur:
            warning_columns = {str(row["name"]) for row in await cur.fetchall()}
        if "excluded_from_history" not in warning_columns:
            await self.db.execute(
                "ALTER TABLE warnings ADD COLUMN excluded_from_history INTEGER NOT NULL DEFAULT 0"
            )

        async with self.db.execute("PRAGMA table_info(timeouts)") as cur:
            timeout_columns = {str(row["name"]) for row in await cur.fetchall()}
        if "cleanup_minutes" not in timeout_columns:
            await self.db.execute(
                "ALTER TABLE timeouts ADD COLUMN cleanup_minutes INTEGER NOT NULL DEFAULT 0"
            )
        if "messages_deleted" not in timeout_columns:
            await self.db.execute(
                "ALTER TABLE timeouts ADD COLUMN messages_deleted INTEGER NOT NULL DEFAULT 0"
            )
        if "excluded_from_history" not in timeout_columns:
            await self.db.execute(
                "ALTER TABLE timeouts ADD COLUMN excluded_from_history INTEGER NOT NULL DEFAULT 0"
            )

    async def ensure_guild(self, guild_id: int) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO guild_settings(guild_id) VALUES (?)",
            (guild_id,),
        )
        await self.db.commit()

    async def get_guild_settings(self, guild_id: int) -> dict[str, Any]:
        await self.ensure_guild(guild_id)
        async with self.db.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        return dict(row)

    async def set_log_channel(self, guild_id: int, channel_id: int | None) -> None:
        await self.ensure_guild(guild_id)
        await self.db.execute(
            "UPDATE guild_settings SET mod_log_channel_id = ? WHERE guild_id = ?",
            (channel_id, guild_id),
        )
        await self.db.commit()

    async def set_timeout_cleanup_minutes(self, guild_id: int, minutes: int) -> None:
        await self.ensure_guild(guild_id)
        await self.db.execute(
            "UPDATE guild_settings SET timeout_cleanup_minutes = ? WHERE guild_id = ?",
            (minutes, guild_id),
        )
        await self.db.commit()

    async def set_dm_setting(self, guild_id: int, setting: str, enabled: bool) -> None:
        if setting not in {"dm_warnings", "dm_timeouts"}:
            raise ValueError("Unknown DM setting")
        await self.ensure_guild(guild_id)
        await self.db.execute(
            f"UPDATE guild_settings SET {setting} = ? WHERE guild_id = ?",
            (int(enabled), guild_id),
        )
        await self.db.commit()

    async def upsert_role_profile(self, profile: RolePermissions) -> None:
        await self.db.execute(
            """
            INSERT INTO role_permissions(
                guild_id, role_id, can_timeout, can_warn, can_view_warnings,
                can_check, can_untimeout, can_manage_settings, max_timeout_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, role_id) DO UPDATE SET
                can_timeout = excluded.can_timeout,
                can_warn = excluded.can_warn,
                can_view_warnings = excluded.can_view_warnings,
                can_check = excluded.can_check,
                can_untimeout = excluded.can_untimeout,
                can_manage_settings = excluded.can_manage_settings,
                max_timeout_seconds = excluded.max_timeout_seconds
            """,
            (
                profile.guild_id,
                profile.role_id,
                int(profile.can_timeout),
                int(profile.can_warn),
                int(profile.can_view_warnings),
                int(profile.can_check),
                int(profile.can_untimeout),
                int(profile.can_manage_settings),
                profile.max_timeout_seconds,
            ),
        )
        await self.db.commit()

    async def ensure_role_profile(self, guild_id: int, role_id: int) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO role_permissions(guild_id, role_id) VALUES (?, ?)",
            (guild_id, role_id),
        )
        await self.db.commit()

    async def set_role_permission(
        self, guild_id: int, role_id: int, permission: str, enabled: bool
    ) -> None:
        column = self.PERMISSION_COLUMNS.get(permission)
        if column is None:
            raise ValueError("Unknown permission")
        await self.ensure_role_profile(guild_id, role_id)
        await self.db.execute(
            f"UPDATE role_permissions SET {column} = ? WHERE guild_id = ? AND role_id = ?",
            (int(enabled), guild_id, role_id),
        )
        await self.db.commit()

    async def set_role_timeout_limit(self, guild_id: int, role_id: int, seconds: int) -> None:
        await self.ensure_role_profile(guild_id, role_id)
        await self.db.execute(
            "UPDATE role_permissions SET max_timeout_seconds = ? WHERE guild_id = ? AND role_id = ?",
            (seconds, guild_id, role_id),
        )
        await self.db.commit()

    async def get_role_profile(self, guild_id: int, role_id: int) -> RolePermissions | None:
        async with self.db.execute(
            "SELECT * FROM role_permissions WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return self._row_to_profile(row)

    async def get_role_profiles(
        self, guild_id: int, role_ids: Iterable[int]
    ) -> dict[int, RolePermissions]:
        ids = list(role_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        params = [guild_id, *ids]
        async with self.db.execute(
            f"SELECT * FROM role_permissions WHERE guild_id = ? AND role_id IN ({placeholders})",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return {int(row["role_id"]): self._row_to_profile(row) for row in rows}

    @staticmethod
    def _row_to_profile(row: aiosqlite.Row) -> RolePermissions:
        return RolePermissions(
            guild_id=int(row["guild_id"]),
            role_id=int(row["role_id"]),
            can_timeout=bool(row["can_timeout"]),
            can_warn=bool(row["can_warn"]),
            can_view_warnings=bool(row["can_view_warnings"]),
            can_check=bool(row["can_check"]),
            can_untimeout=bool(row["can_untimeout"]),
            can_manage_settings=bool(row["can_manage_settings"]),
            max_timeout_seconds=int(row["max_timeout_seconds"]),
        )

    async def add_warning(
        self, guild_id: int, user_id: int, moderator_id: int, reason: str
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            cur = await self.db.execute(
                """
                INSERT INTO warnings(guild_id, user_id, moderator_id, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, user_id, moderator_id, reason, now),
            )
            await self.db.commit()
            return int(cur.lastrowid)

    async def list_warnings(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        async with self.db.execute(
            """
            SELECT * FROM warnings
            WHERE guild_id = ? AND user_id = ? AND excluded_from_history = 0
            ORDER BY created_at DESC, id DESC
            """,
            (guild_id, user_id),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def add_timeout(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        duration_seconds: int,
        reason: str,
        started_at: datetime,
        expires_at: datetime,
    ) -> int:
        async with self._lock:
            cur = await self.db.execute(
                """
                INSERT INTO timeouts(
                    guild_id, user_id, moderator_id, duration_seconds, reason,
                    started_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    user_id,
                    moderator_id,
                    duration_seconds,
                    reason,
                    started_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            await self.db.commit()
            return int(cur.lastrowid)

    async def set_timeout_cleanup_result(
        self, timeout_id: int, cleanup_minutes: int, messages_deleted: int
    ) -> None:
        await self.db.execute(
            "UPDATE timeouts SET cleanup_minutes = ?, messages_deleted = ? WHERE id = ?",
            (cleanup_minutes, messages_deleted, timeout_id),
        )
        await self.db.commit()

    async def end_latest_timeout(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        reason: str,
    ) -> int | None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            async with self.db.execute(
                """
                SELECT id FROM timeouts
                WHERE guild_id = ? AND user_id = ? AND ended_at IS NULL
                ORDER BY started_at DESC, id DESC LIMIT 1
                """,
                (guild_id, user_id),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            timeout_id = int(row["id"])
            await self.db.execute(
                """
                UPDATE timeouts
                SET ended_at = ?, ended_by = ?, end_reason = ?, excluded_from_history = 1
                WHERE id = ?
                """,
                (now, moderator_id, reason, timeout_id),
            )
            await self.db.commit()
            return timeout_id

    async def list_moderation_history(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        """Return active /check history, newest first, with warnings and timeouts merged."""
        async with self.db.execute(
            """
            SELECT id, guild_id, user_id, moderator_id, reason, created_at AS event_at,
                   'warning' AS kind, NULL AS duration_seconds, NULL AS cleanup_minutes,
                   NULL AS messages_deleted
            FROM warnings
            WHERE guild_id = ? AND user_id = ? AND excluded_from_history = 0
            UNION ALL
            SELECT id, guild_id, user_id, moderator_id, reason, started_at AS event_at,
                   'timeout' AS kind, duration_seconds, cleanup_minutes, messages_deleted
            FROM timeouts
            WHERE guild_id = ? AND user_id = ? AND excluded_from_history = 0
            ORDER BY event_at DESC, id DESC
            """,
            (guild_id, user_id, guild_id, user_id),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def exclude_history_entry(
        self, guild_id: int, user_id: int, kind: str, case_id: int
    ) -> dict[str, Any] | None:
        """Soft-remove one warning/timeout from user-facing moderation history."""
        if kind not in {"warning", "timeout"}:
            raise ValueError("Unknown moderation history kind")
        table = "warnings" if kind == "warning" else "timeouts"
        async with self._lock:
            async with self.db.execute(
                f"SELECT * FROM {table} WHERE id = ? AND guild_id = ? AND user_id = ? AND excluded_from_history = 0",
                (case_id, guild_id, user_id),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            await self.db.execute(
                f"UPDATE {table} SET excluded_from_history = 1 WHERE id = ? AND guild_id = ? AND user_id = ?",
                (case_id, guild_id, user_id),
            )
            await self.db.commit()
            result = dict(row)
            result["kind"] = kind
            return result

    async def clear_moderation_history(self, guild_id: int, user_id: int) -> dict[str, int]:
        """Soft-clear all active /check history while keeping audit rows in SQLite."""
        async with self._lock:
            async with self.db.execute(
                "SELECT COUNT(*) AS count FROM warnings WHERE guild_id = ? AND user_id = ? AND excluded_from_history = 0",
                (guild_id, user_id),
            ) as cur:
                warning_count = int((await cur.fetchone())["count"])
            async with self.db.execute(
                "SELECT COUNT(*) AS count FROM timeouts WHERE guild_id = ? AND user_id = ? AND excluded_from_history = 0",
                (guild_id, user_id),
            ) as cur:
                timeout_count = int((await cur.fetchone())["count"])
            await self.db.execute(
                "UPDATE warnings SET excluded_from_history = 1 WHERE guild_id = ? AND user_id = ? AND excluded_from_history = 0",
                (guild_id, user_id),
            )
            await self.db.execute(
                "UPDATE timeouts SET excluded_from_history = 1 WHERE guild_id = ? AND user_id = ? AND excluded_from_history = 0",
                (guild_id, user_id),
            )
            await self.db.commit()
        return {"warnings": warning_count, "timeouts": timeout_count}

    async def get_moderation_summary(self, guild_id: int, user_id: int) -> dict[str, Any]:
        async with self.db.execute(
            "SELECT COUNT(*) AS count FROM warnings WHERE guild_id = ? AND user_id = ? AND excluded_from_history = 0",
            (guild_id, user_id),
        ) as cur:
            warning_count = int((await cur.fetchone())["count"])

        async with self.db.execute(
            """
            SELECT COUNT(*) AS count, COALESCE(SUM(duration_seconds), 0) AS total_seconds
            FROM timeouts
            WHERE guild_id = ? AND user_id = ? AND excluded_from_history = 0
            """,
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            timeout_count = int(row["count"])
            total_timeout_seconds = int(row["total_seconds"])

        async with self.db.execute(
            """
            SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? AND excluded_from_history = 0
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (guild_id, user_id),
        ) as cur:
            last_warning = await cur.fetchone()

        async with self.db.execute(
            """
            SELECT * FROM timeouts
            WHERE guild_id = ? AND user_id = ? AND excluded_from_history = 0
            ORDER BY started_at DESC, id DESC LIMIT 1
            """,
            (guild_id, user_id),
        ) as cur:
            last_timeout = await cur.fetchone()

        return {
            "warning_count": warning_count,
            "timeout_count": timeout_count,
            "total_timeout_seconds": total_timeout_seconds,
            "last_warning": dict(last_warning) if last_warning else None,
            "last_timeout": dict(last_timeout) if last_timeout else None,
        }

    async def record_message(
        self,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
        message_id: int,
        content: str,
        attachment_count: int,
        created_at: datetime,
        edited_at: datetime | None = None,
        retain_per_user: int = 250,
    ) -> None:
        """Store/update a recent guild message used by /history.

        The table is intentionally bounded per user so message indexing does not grow
        forever on an active Discord server.
        """
        async with self._lock:
            await self.db.execute(
                """
                INSERT INTO message_history(
                    message_id, guild_id, channel_id, user_id, content,
                    attachment_count, created_at, edited_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(message_id) DO UPDATE SET
                    guild_id = excluded.guild_id,
                    channel_id = excluded.channel_id,
                    user_id = excluded.user_id,
                    content = excluded.content,
                    attachment_count = excluded.attachment_count,
                    created_at = excluded.created_at,
                    edited_at = excluded.edited_at,
                    deleted_at = NULL
                """,
                (
                    message_id,
                    guild_id,
                    channel_id,
                    user_id,
                    content,
                    attachment_count,
                    created_at.isoformat(),
                    edited_at.isoformat() if edited_at is not None else None,
                ),
            )
            # Keep only the newest N messages for this user in this guild.
            if retain_per_user > 0:
                await self.db.execute(
                    """
                    DELETE FROM message_history
                    WHERE guild_id = ? AND user_id = ? AND message_id NOT IN (
                        SELECT message_id FROM message_history
                        WHERE guild_id = ? AND user_id = ?
                        ORDER BY created_at DESC, message_id DESC
                        LIMIT ?
                    )
                    """,
                    (guild_id, user_id, guild_id, user_id, retain_per_user),
                )
            await self.db.commit()

    async def mark_message_deleted(self, guild_id: int, message_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "UPDATE message_history SET deleted_at = ? WHERE guild_id = ? AND message_id = ?",
            (now, guild_id, message_id),
        )
        await self.db.commit()

    async def mark_messages_deleted(self, guild_id: int, message_ids: Iterable[int]) -> None:
        ids = [int(message_id) for message_id in message_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            f"UPDATE message_history SET deleted_at = ? WHERE guild_id = ? AND message_id IN ({placeholders})",
            [now, guild_id, *ids],
        )
        await self.db.commit()

    async def list_recent_messages(
        self, guild_id: int, user_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        async with self.db.execute(
            """
            SELECT message_id, guild_id, channel_id, user_id, content,
                   attachment_count, created_at, edited_at, deleted_at
            FROM message_history
            WHERE guild_id = ? AND user_id = ?
            ORDER BY created_at DESC, message_id DESC
            LIMIT ?
            """,
            (guild_id, user_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

