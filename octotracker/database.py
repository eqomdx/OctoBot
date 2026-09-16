from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import aiosqlite

from .announcements import Announcement
from .auth import (
    MALFORMED_RESPONSE,
    RESPONSIVE,
    UNKNOWN as AUTH_UNKNOWN,
    UNRESPONSIVE,
    AuthProbeResult,
    AuthState,
    AuthTransition,
)
from .connectivity import (
    REACHABLE,
    UNKNOWN as CONNECTIVITY_UNKNOWN,
    UNREACHABLE,
    ConnectivityResult,
    ConnectivityState,
)
from .permissions import MANAGED_COMMANDS
from .reports import CommunityReport, ReportGroup, ReportSummary, validate_report
from .radio import (
    LIVE as RADIO_LIVE,
    RadioShow,
    RadioSnapshot,
    RadioState,
    RadioTransition,
    fallback_occurrence_key,
    radio_identity,
    same_broadcaster,
)
from .status import (
    OFFLINE,
    ONLINE,
    PARTIAL,
    REALMS,
    UNKNOWN,
    RealmState,
    ServiceTransition,
    StatusResult,
    StatusSnapshot,
    overall_status,
)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


# A DJ who drops and reconnects within this window is treated as resuming the same
# show rather than starting a new one, so the channel is not pinged twice.
RADIO_RESUME_GRACE = timedelta(minutes=15)


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class UptimeStats:
    window_seconds: int
    online_seconds: float
    observed_seconds: float

    @property
    def percentage(self) -> float | None:
        if self.observed_seconds <= 0:
            return None
        return self.online_seconds / self.observed_seconds * 100


@dataclass(frozen=True, slots=True)
class Incident:
    incident_id: int
    started_at: datetime
    ended_at: datetime | None
    initial_status: str
    worst_status: str

    @property
    def duration_seconds(self) -> float:
        end = self.ended_at or datetime.now(timezone.utc)
        return max(0.0, (end - self.started_at).total_seconds())


@dataclass(frozen=True, slots=True)
class IncidentNotification:
    kind: str
    incident: Incident


@dataclass(frozen=True, slots=True)
class AuthIncident:
    incident_id: int
    started_at: datetime
    ended_at: datetime | None
    initial_status: str
    latest_status: str
    tcp_status: str
    reason: str | None
    latency_ms: int | None

    @property
    def duration_seconds(self) -> float:
        end = self.ended_at or datetime.now(timezone.utc)
        return max(0.0, (end - self.started_at).total_seconds())


@dataclass(frozen=True, slots=True)
class AuthIncidentNotification:
    kind: str
    incident: AuthIncident


@dataclass(frozen=True, slots=True)
class RadioOccurrence:
    occurrence_key: str
    station_identifier: str
    source_event_id: str | None
    title: str
    presenter: str | None
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    detected_live_at: datetime
    detected_end_at: datetime | None
    description: str | None
    artwork_url: str | None
    notification_sent: bool
    notification_message_id: int | None


@dataclass(frozen=True, slots=True)
class ReportIncident:
    incident_id: int
    guild_id: int
    started_at: datetime
    ended_at: datetime | None
    peak_unique_users: int
    latest_unique_users: int

    @property
    def duration_seconds(self) -> float:
        end = self.ended_at or datetime.now(timezone.utc)
        return max(0.0, (end - self.started_at).total_seconds())


@dataclass(frozen=True, slots=True)
class ReportIncidentNotification:
    kind: str
    incident: ReportIncident


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS realm_state (
    realm TEXT PRIMARY KEY,
    confirmed_status TEXT NOT NULL DEFAULT 'unknown',
    pending_status TEXT,
    pending_count INTEGER NOT NULL DEFAULT 0,
    status_since TEXT,
    last_observed_status TEXT NOT NULL DEFAULT 'unknown',
    last_checked_at TEXT,
    last_success_at TEXT
);

CREATE TABLE IF NOT EXISTS realm_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    realm TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_realm_history_window
ON realm_history(realm, started_at, ended_at);

CREATE TABLE IF NOT EXISTS service_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    confirmed_status TEXT NOT NULL DEFAULT 'unknown',
    status_since TEXT,
    last_checked_at TEXT,
    last_success_at TEXT,
    source_ok INTEGER NOT NULL DEFAULT 0,
    source_error TEXT
);

CREATE TABLE IF NOT EXISTS service_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_service_history_window
ON service_history(started_at, ended_at);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    initial_status TEXT NOT NULL,
    worst_status TEXT NOT NULL,
    outage_alert_sent INTEGER NOT NULL DEFAULT 0,
    recovery_alert_sent INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_incidents_started
ON incidents(started_at DESC);

CREATE TABLE IF NOT EXISTS announcements (
    topic_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT,
    published_at TEXT,
    preview TEXT,
    body TEXT,
    content_source TEXT,
    url TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    announced INTEGER NOT NULL DEFAULT 0,
    delivery_pages_sent INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS community_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    realm TEXT NOT NULL,
    issue TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    cleared_at TEXT,
    cleared_by_user_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_community_reports_active
ON community_reports(guild_id, updated_at, cleared_at);

CREATE INDEX IF NOT EXISTS idx_community_reports_refresh
ON community_reports(guild_id, user_id, realm, issue, updated_at);

CREATE TABLE IF NOT EXISTS command_roles (
    guild_id INTEGER NOT NULL,
    command_name TEXT NOT NULL,
    role_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    created_by_user_id INTEGER NOT NULL,
    PRIMARY KEY(guild_id, command_name, role_id)
);

CREATE INDEX IF NOT EXISTS idx_command_roles_lookup
ON command_roles(guild_id, command_name);

CREATE TABLE IF NOT EXISTS connectivity_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    confirmed_status TEXT NOT NULL DEFAULT 'unknown',
    pending_status TEXT,
    pending_count INTEGER NOT NULL DEFAULT 0,
    status_since TEXT,
    last_checked_at TEXT,
    last_success_at TEXT,
    error TEXT,
    host TEXT NOT NULL DEFAULT 'play.octowow.st',
    port INTEGER
);

CREATE TABLE IF NOT EXISTS connectivity_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_connectivity_history_window
ON connectivity_history(started_at, ended_at);

CREATE TABLE IF NOT EXISTS auth_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    confirmed_status TEXT NOT NULL DEFAULT 'unknown',
    pending_status TEXT,
    pending_count INTEGER NOT NULL DEFAULT 0,
    last_observed_status TEXT NOT NULL DEFAULT 'unknown',
    tcp_status TEXT NOT NULL DEFAULT 'unknown',
    status_since TEXT,
    last_checked_at TEXT,
    last_success_at TEXT,
    last_failure_at TEXT,
    latency_ms INTEGER,
    consecutive_successes INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    host TEXT NOT NULL DEFAULT 'play.octowow.st',
    port INTEGER
);

CREATE TABLE IF NOT EXISTS auth_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    old_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    tcp_status TEXT NOT NULL,
    latency_ms INTEGER,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_history_timestamp
ON auth_history(timestamp DESC);

CREATE TABLE IF NOT EXISTS auth_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    initial_status TEXT NOT NULL,
    latest_status TEXT NOT NULL,
    tcp_status TEXT NOT NULL,
    reason TEXT,
    last_latency_ms INTEGER,
    degraded_alert_sent INTEGER NOT NULL DEFAULT 0,
    recovery_alert_sent INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_auth_incidents_started
ON auth_incidents(started_at DESC);

CREATE TABLE IF NOT EXISTS radio_state (
    station_identifier TEXT PRIMARY KEY,
    current_state TEXT NOT NULL DEFAULT 'unknown',
    current_occurrence_key TEXT,
    current_identity TEXT,
    current_title TEXT,
    current_presenter TEXT,
    scheduled_start TEXT,
    scheduled_end TEXT,
    detected_live_at TEXT,
    pending_occurrence_key TEXT,
    pending_identity TEXT,
    pending_count INTEGER NOT NULL DEFAULT 0,
    missing_count INTEGER NOT NULL DEFAULT 0,
    last_checked_at TEXT,
    last_success_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS radio_show_history (
    occurrence_key TEXT PRIMARY KEY,
    station_identifier TEXT NOT NULL,
    source_event_id TEXT,
    title TEXT NOT NULL,
    presenter TEXT,
    scheduled_start TEXT,
    scheduled_end TEXT,
    detected_live_at TEXT NOT NULL,
    detected_end_at TEXT,
    description TEXT,
    artwork_url TEXT,
    notification_sent INTEGER NOT NULL DEFAULT 0,
    notification_message_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_radio_history_station_time
ON radio_show_history(station_identifier, detected_live_at DESC);

CREATE TABLE IF NOT EXISTS radio_schedule_announcements (
    occurrence_key TEXT PRIMARY KEY,
    station_identifier TEXT NOT NULL,
    title TEXT NOT NULL,
    presenter TEXT,
    scheduled_start TEXT NOT NULL,
    scheduled_end TEXT,
    announced_at TEXT NOT NULL,
    message_id INTEGER
);

CREATE TABLE IF NOT EXISTS report_alert_state (
    guild_id INTEGER PRIMARY KEY,
    confirmed_degraded INTEGER NOT NULL DEFAULT 0,
    below_threshold_count INTEGER NOT NULL DEFAULT 0,
    current_incident_id INTEGER,
    last_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS report_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    peak_unique_users INTEGER NOT NULL,
    latest_unique_users INTEGER NOT NULL,
    alert_sent INTEGER NOT NULL DEFAULT 0,
    recovery_sent INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_report_incidents_guild_started
ON report_incidents(guild_id, started_at DESC);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.executescript(SCHEMA)
        await self._migrate_schema()

        for realm in REALMS:
            await self.connection.execute(
                "INSERT OR IGNORE INTO realm_state(realm) VALUES (?)", (realm,)
            )
        await self.connection.execute(
            "INSERT OR IGNORE INTO service_state(id) VALUES (1)"
        )
        await self.connection.execute(
            "INSERT OR IGNORE INTO connectivity_state(id) VALUES (1)"
        )
        await self.connection.execute(
            "INSERT OR IGNORE INTO auth_state(id) VALUES (1)"
        )
        await self.connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES ('schema_version', '4')
            ON CONFLICT(key) DO UPDATE SET value = '4'
            """
        )
        await self.connection.commit()

    async def _migrate_schema(self) -> None:
        """Add later-version columns without rebuilding or discarding tables."""
        await self._ensure_column(
            "incidents", "outage_alert_sent", "INTEGER NOT NULL DEFAULT 0"
        )
        await self._ensure_column(
            "incidents", "recovery_alert_sent", "INTEGER NOT NULL DEFAULT 0"
        )
        await self._ensure_column("announcements", "body", "TEXT")
        await self._ensure_column("announcements", "content_source", "TEXT")
        await self._ensure_column(
            "announcements",
            "delivery_pages_sent",
            "INTEGER NOT NULL DEFAULT 0",
        )

    async def _ensure_column(
        self, table: str, column: str, declaration: str
    ) -> None:
        connection = self._connection()
        cursor = await connection.execute(f"PRAGMA table_info({table})")
        columns = {row["name"] for row in await cursor.fetchall()}
        if column not in columns:
            await connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    def _connection(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("Database has not been connected")
        return self.connection

    async def get_metadata(self, key: str) -> str | None:
        async with self._lock:
            cursor = await self._connection().execute(
                "SELECT value FROM metadata WHERE key = ?", (key,)
            )
            row = await cursor.fetchone()
            return row["value"] if row else None

    async def set_metadata(self, key: str, value: str) -> None:
        async with self._lock:
            await self._connection().execute(
                """
                INSERT INTO metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            await self._connection().commit()

    async def apply_status_result(
        self, result: StatusResult, confirmation_checks: int
    ) -> tuple[StatusSnapshot, ServiceTransition | None]:
        checked_at = _iso(result.checked_at)
        source_ok = result.complete

        async with self._lock:
            connection = self._connection()
            cursor = await connection.execute(
                "SELECT * FROM realm_state ORDER BY rowid"
            )
            rows = {row["realm"]: row for row in await cursor.fetchall()}
            for realm in REALMS:
                row = rows[realm]
                observed = result.realms.get(realm, UNKNOWN)
                confirmed = row["confirmed_status"]
                pending = row["pending_status"]
                pending_count = row["pending_count"]
                status_since = row["status_since"]

                if observed not in {ONLINE, OFFLINE}:
                    pending = None
                    pending_count = 0
                elif confirmed == UNKNOWN:
                    confirmed = observed
                    pending = None
                    pending_count = 0
                    status_since = checked_at
                    await connection.execute(
                        """
                        INSERT INTO realm_history(realm, status, started_at)
                        VALUES (?, ?, ?)
                        """,
                        (realm, confirmed, checked_at),
                    )
                elif observed == confirmed:
                    pending = None
                    pending_count = 0
                else:
                    if pending == observed:
                        pending_count += 1
                    else:
                        pending = observed
                        pending_count = 1

                    if pending_count >= confirmation_checks:
                        await connection.execute(
                            """
                            UPDATE realm_history SET ended_at = ?
                            WHERE realm = ? AND ended_at IS NULL
                            """,
                            (checked_at, realm),
                        )
                        confirmed = observed
                        status_since = checked_at
                        pending = None
                        pending_count = 0
                        await connection.execute(
                            """
                            INSERT INTO realm_history(realm, status, started_at)
                            VALUES (?, ?, ?)
                            """,
                            (realm, confirmed, checked_at),
                        )

                await connection.execute(
                    """
                    UPDATE realm_state
                    SET confirmed_status = ?, pending_status = ?, pending_count = ?,
                        status_since = ?, last_observed_status = ?,
                        last_checked_at = ?,
                        last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END
                    WHERE realm = ?
                    """,
                    (
                        confirmed,
                        pending,
                        pending_count,
                        status_since,
                        observed,
                        checked_at,
                        observed in {ONLINE, OFFLINE},
                        checked_at,
                        realm,
                    ),
                )

            cursor = await connection.execute(
                "SELECT realm, confirmed_status FROM realm_state ORDER BY rowid"
            )
            new_realm_statuses = {
                row["realm"]: row["confirmed_status"]
                for row in await cursor.fetchall()
            }
            new_overall = overall_status(new_realm_statuses)

            cursor = await connection.execute(
                "SELECT * FROM service_state WHERE id = 1"
            )
            service_row = await cursor.fetchone()
            old_overall = service_row["confirmed_status"]
            overall_since = service_row["status_since"]
            transition: ServiceTransition | None = None

            if new_overall != UNKNOWN and new_overall != old_overall:
                if old_overall != UNKNOWN:
                    await connection.execute(
                        "UPDATE service_history SET ended_at = ? WHERE ended_at IS NULL",
                        (checked_at,),
                    )
                await connection.execute(
                    "INSERT INTO service_history(status, started_at) VALUES (?, ?)",
                    (new_overall, checked_at),
                )
                if not (
                    old_overall in {PARTIAL, OFFLINE}
                    and new_overall in {PARTIAL, OFFLINE}
                ):
                    # Keep the interruption start while an incident changes
                    # between partial and total outage.
                    overall_since = checked_at

                kind: str | None = None
                incident_id: int | None = None
                duration_seconds: float | None = None

                if old_overall == ONLINE and new_overall in {PARTIAL, OFFLINE}:
                    cursor = await connection.execute(
                        """
                        INSERT INTO incidents(started_at, initial_status, worst_status)
                        VALUES (?, ?, ?)
                        """,
                        (checked_at, new_overall, new_overall),
                    )
                    incident_id = cursor.lastrowid
                    kind = "outage"
                elif old_overall in {PARTIAL, OFFLINE} and new_overall == ONLINE:
                    cursor = await connection.execute(
                        """
                        SELECT id, started_at FROM incidents
                        WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1
                        """
                    )
                    incident = await cursor.fetchone()
                    if incident:
                        incident_id = incident["id"]
                        started_at = _datetime(incident["started_at"])
                        if started_at is not None:
                            duration_seconds = max(
                                0.0, (result.checked_at - started_at).total_seconds()
                            )
                        await connection.execute(
                            "UPDATE incidents SET ended_at = ? WHERE id = ?",
                            (checked_at, incident_id),
                        )
                    kind = "recovery"
                elif old_overall in {PARTIAL, OFFLINE} and new_overall == OFFLINE:
                    await connection.execute(
                        """
                        UPDATE incidents SET worst_status = ?
                        WHERE ended_at IS NULL
                        """,
                        (OFFLINE,),
                    )
                elif old_overall == UNKNOWN and new_overall in {PARTIAL, OFFLINE}:
                    # Record downtime discovered on a fresh database, but do not
                    # emit a transition alert without a known-online baseline.
                    cursor = await connection.execute(
                        """
                        INSERT INTO incidents(
                            started_at, initial_status, worst_status, outage_alert_sent
                        ) VALUES (?, ?, ?, 1)
                        """,
                        (checked_at, new_overall, new_overall),
                    )
                    incident_id = cursor.lastrowid

                transition = ServiceTransition(
                    kind=kind,
                    old_status=old_overall,
                    new_status=new_overall,
                    changed_at=result.checked_at,
                    incident_id=incident_id,
                    duration_seconds=duration_seconds,
                )

            if new_overall == UNKNOWN:
                new_overall = old_overall

            await connection.execute(
                """
                UPDATE service_state
                SET confirmed_status = ?, status_since = ?, last_checked_at = ?,
                    last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,
                    source_ok = ?, source_error = ?
                WHERE id = 1
                """,
                (
                    new_overall,
                    overall_since,
                    checked_at,
                    source_ok,
                    checked_at,
                    source_ok,
                    result.error,
                ),
            )
            await connection.commit()
            snapshot = await self._get_status_snapshot_unlocked()
            return snapshot, transition

    async def get_status_snapshot(self) -> StatusSnapshot:
        async with self._lock:
            return await self._get_status_snapshot_unlocked()

    async def _get_status_snapshot_unlocked(self) -> StatusSnapshot:
        connection = self._connection()
        cursor = await connection.execute(
            "SELECT * FROM realm_state ORDER BY rowid"
        )
        realm_rows = await cursor.fetchall()
        realms = tuple(
            RealmState(
                name=row["realm"],
                status=row["confirmed_status"],
                status_since=_datetime(row["status_since"]),
                pending_status=row["pending_status"],
                pending_count=row["pending_count"],
                last_observed_status=row["last_observed_status"],
            )
            for row in realm_rows
        )

        cursor = await connection.execute(
            "SELECT * FROM service_state WHERE id = 1"
        )
        row = await cursor.fetchone()
        return StatusSnapshot(
            overall_status=row["confirmed_status"],
            status_since=_datetime(row["status_since"]),
            realms=realms,
            checked_at=_datetime(row["last_checked_at"]),
            last_success_at=_datetime(row["last_success_at"]),
            source_ok=bool(row["source_ok"]),
            source_error=row["source_error"],
        )

    async def apply_connectivity_result(
        self, result: ConnectivityResult, confirmation_checks: int
    ) -> ConnectivityState:
        checked_at = _iso(result.checked_at)
        async with self._lock:
            connection = self._connection()
            cursor = await connection.execute(
                "SELECT * FROM connectivity_state WHERE id = 1"
            )
            row = await cursor.fetchone()
            confirmed = row["confirmed_status"]
            pending = row["pending_status"]
            pending_count = row["pending_count"]
            status_since = row["status_since"]

            if result.status == CONNECTIVITY_UNKNOWN:
                if confirmed != CONNECTIVITY_UNKNOWN:
                    await connection.execute(
                        "UPDATE connectivity_history SET ended_at = ? "
                        "WHERE ended_at IS NULL",
                        (checked_at,),
                    )
                    await connection.execute(
                        "INSERT INTO connectivity_history(status, started_at) "
                        "VALUES (?, ?)",
                        (CONNECTIVITY_UNKNOWN, checked_at),
                    )
                    status_since = checked_at
                confirmed = CONNECTIVITY_UNKNOWN
                pending = None
                pending_count = 0
            elif confirmed == CONNECTIVITY_UNKNOWN and result.status == REACHABLE:
                confirmed = REACHABLE
                pending = None
                pending_count = 0
                status_since = checked_at
                await connection.execute(
                    "UPDATE connectivity_history SET ended_at = ? "
                    "WHERE ended_at IS NULL",
                    (checked_at,),
                )
                await connection.execute(
                    "INSERT INTO connectivity_history(status, started_at) "
                    "VALUES (?, ?)",
                    (confirmed, checked_at),
                )
            elif result.status == confirmed:
                pending = None
                pending_count = 0
            elif result.status in {REACHABLE, UNREACHABLE}:
                if pending == result.status:
                    pending_count += 1
                else:
                    pending = result.status
                    pending_count = 1
                if pending_count >= confirmation_checks:
                    await connection.execute(
                        "UPDATE connectivity_history SET ended_at = ? "
                        "WHERE ended_at IS NULL",
                        (checked_at,),
                    )
                    confirmed = result.status
                    pending = None
                    pending_count = 0
                    status_since = checked_at
                    await connection.execute(
                        "INSERT INTO connectivity_history(status, started_at) "
                        "VALUES (?, ?)",
                        (confirmed, checked_at),
                    )

            await connection.execute(
                """
                UPDATE connectivity_state
                SET confirmed_status = ?, pending_status = ?, pending_count = ?,
                    status_since = ?, last_checked_at = ?,
                    last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,
                    error = ?, host = ?, port = ?
                WHERE id = 1
                """,
                (
                    confirmed,
                    pending,
                    pending_count,
                    status_since,
                    checked_at,
                    result.status == REACHABLE,
                    checked_at,
                    result.error,
                    result.host,
                    result.port,
                ),
            )
            await connection.commit()
            return await self._get_connectivity_state_unlocked()

    async def get_connectivity_state(self) -> ConnectivityState:
        async with self._lock:
            return await self._get_connectivity_state_unlocked()

    async def _get_connectivity_state_unlocked(self) -> ConnectivityState:
        cursor = await self._connection().execute(
            "SELECT * FROM connectivity_state WHERE id = 1"
        )
        row = await cursor.fetchone()
        return ConnectivityState(
            status=row["confirmed_status"],
            status_since=_datetime(row["status_since"]),
            pending_status=row["pending_status"],
            pending_count=row["pending_count"],
            checked_at=_datetime(row["last_checked_at"]),
            last_success_at=_datetime(row["last_success_at"]),
            error=row["error"],
            host=row["host"],
            port=row["port"],
        )

    async def apply_auth_result(
        self,
        result: AuthProbeResult,
        failure_confirmations: int,
        recovery_confirmations: int,
    ) -> tuple[AuthState, AuthTransition | None]:
        if failure_confirmations <= 0 or recovery_confirmations <= 0:
            raise ValueError("Authentication confirmation counts must be positive")
        if result.auth_status not in {
            RESPONSIVE,
            UNRESPONSIVE,
            MALFORMED_RESPONSE,
            AUTH_UNKNOWN,
        }:
            raise ValueError("Unknown authentication state")

        checked_at = _iso(result.checked_at)
        unhealthy = {UNRESPONSIVE, MALFORMED_RESPONSE}
        transition: AuthTransition | None = None

        async with self._lock:
            connection = self._connection()
            cursor = await connection.execute("SELECT * FROM auth_state WHERE id = 1")
            row = await cursor.fetchone()
            confirmed = row["confirmed_status"]
            old_confirmed = confirmed
            pending = row["pending_status"]
            pending_count = row["pending_count"]
            status_since = row["status_since"]
            consecutive_successes = row["consecutive_successes"]
            consecutive_failures = row["consecutive_failures"]

            if result.auth_status == AUTH_UNKNOWN:
                pending = None
                pending_count = 0
                consecutive_successes = 0
                consecutive_failures = 0
            elif result.auth_status == RESPONSIVE:
                consecutive_successes += 1
                consecutive_failures = 0
                if confirmed == RESPONSIVE:
                    pending = None
                    pending_count = 0
                else:
                    if pending == RESPONSIVE:
                        pending_count += 1
                    else:
                        pending = RESPONSIVE
                        pending_count = 1
                    if pending_count >= recovery_confirmations:
                        confirmed = RESPONSIVE
                        pending = None
                        pending_count = 0
            else:
                consecutive_failures += 1
                consecutive_successes = 0
                if confirmed in unhealthy:
                    # The endpoint is already confirmed unhealthy. Preserve one
                    # incident while updating its most precise current diagnosis.
                    confirmed = result.auth_status
                    pending = None
                    pending_count = 0
                elif pending in unhealthy:
                    pending = result.auth_status
                    pending_count += 1
                else:
                    pending = result.auth_status
                    pending_count = 1
                if confirmed not in unhealthy and pending_count >= failure_confirmations:
                    confirmed = result.auth_status
                    pending = None
                    pending_count = 0

            if confirmed != old_confirmed:
                status_since = checked_at
                await connection.execute(
                    """
                    INSERT INTO auth_history(
                        timestamp, old_status, new_status, tcp_status,
                        latency_ms, reason
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checked_at,
                        old_confirmed,
                        confirmed,
                        result.tcp_status,
                        result.latency_ms,
                        result.reason,
                    ),
                )

                incident_id: int | None = None
                duration_seconds: float | None = None
                transition_kind: str | None = None
                cursor = await connection.execute(
                    """
                    SELECT * FROM auth_incidents
                    WHERE ended_at IS NULL
                    ORDER BY started_at DESC LIMIT 1
                    """
                )
                open_incident = await cursor.fetchone()

                if confirmed in unhealthy:
                    if open_incident is None:
                        cursor = await connection.execute(
                            """
                            INSERT INTO auth_incidents(
                                started_at, initial_status, latest_status,
                                tcp_status, reason, last_latency_ms
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                checked_at,
                                confirmed,
                                confirmed,
                                result.tcp_status,
                                result.reason,
                                result.latency_ms,
                            ),
                        )
                        incident_id = cursor.lastrowid
                        transition_kind = "degraded"
                    else:
                        incident_id = open_incident["id"]
                        await connection.execute(
                            """
                            UPDATE auth_incidents
                            SET latest_status = ?, tcp_status = ?, reason = ?,
                                last_latency_ms = ?
                            WHERE id = ?
                            """,
                            (
                                confirmed,
                                result.tcp_status,
                                result.reason,
                                result.latency_ms,
                                incident_id,
                            ),
                        )
                elif confirmed == RESPONSIVE and open_incident is not None:
                    incident_id = open_incident["id"]
                    started_at = _datetime(open_incident["started_at"])
                    assert started_at is not None
                    duration_seconds = max(
                        0.0, (result.checked_at - started_at).total_seconds()
                    )
                    transition_kind = "recovery"
                    await connection.execute(
                        """
                        UPDATE auth_incidents
                        SET ended_at = ?, tcp_status = ?, reason = ?,
                            last_latency_ms = ?
                        WHERE id = ?
                        """,
                        (
                            checked_at,
                            result.tcp_status,
                            result.reason,
                            result.latency_ms,
                            incident_id,
                        ),
                    )

                transition = AuthTransition(
                    kind=transition_kind,
                    old_status=old_confirmed,
                    new_status=confirmed,
                    changed_at=result.checked_at,
                    incident_id=incident_id,
                    duration_seconds=duration_seconds,
                )
            elif confirmed in unhealthy and result.auth_status in unhealthy:
                # Refresh the active incident even when confirmation state does
                # not change, without creating another incident or alert.
                await connection.execute(
                    """
                    UPDATE auth_incidents
                    SET latest_status = ?, tcp_status = ?, reason = ?,
                        last_latency_ms = ?
                    WHERE ended_at IS NULL
                    """,
                    (
                        confirmed,
                        result.tcp_status,
                        result.reason,
                        result.latency_ms,
                    ),
                )

            await connection.execute(
                """
                UPDATE auth_state
                SET confirmed_status = ?, pending_status = ?, pending_count = ?,
                    last_observed_status = ?, tcp_status = ?, status_since = ?,
                    last_checked_at = ?,
                    last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,
                    last_failure_at = CASE WHEN ? THEN ? ELSE last_failure_at END,
                    latency_ms = ?, consecutive_successes = ?,
                    consecutive_failures = ?, reason = ?, host = ?, port = ?
                WHERE id = 1
                """,
                (
                    confirmed,
                    pending,
                    pending_count,
                    result.auth_status,
                    result.tcp_status,
                    status_since,
                    checked_at,
                    result.auth_status == RESPONSIVE,
                    checked_at,
                    result.auth_status in unhealthy,
                    checked_at,
                    result.latency_ms,
                    consecutive_successes,
                    consecutive_failures,
                    result.reason,
                    result.host,
                    result.port,
                ),
            )
            await connection.commit()
            return await self._get_auth_state_unlocked(), transition

    async def get_auth_state(self) -> AuthState:
        async with self._lock:
            return await self._get_auth_state_unlocked()

    async def _get_auth_state_unlocked(self) -> AuthState:
        cursor = await self._connection().execute("SELECT * FROM auth_state WHERE id = 1")
        row = await cursor.fetchone()
        return AuthState(
            status=row["confirmed_status"],
            status_since=_datetime(row["status_since"]),
            pending_status=row["pending_status"],
            pending_count=row["pending_count"],
            observed_status=row["last_observed_status"],
            tcp_status=row["tcp_status"],
            checked_at=_datetime(row["last_checked_at"]),
            last_success_at=_datetime(row["last_success_at"]),
            last_failure_at=_datetime(row["last_failure_at"]),
            latency_ms=row["latency_ms"],
            consecutive_successes=row["consecutive_successes"],
            consecutive_failures=row["consecutive_failures"],
            reason=row["reason"],
            host=row["host"],
            port=row["port"],
        )

    async def apply_radio_snapshot(
        self, snapshot: RadioSnapshot, confirmation_checks: int
    ) -> tuple[RadioState, RadioTransition | None]:
        if confirmation_checks <= 0:
            raise ValueError("Radio confirmation count must be positive")
        checked_at = _iso(snapshot.checked_at)
        station = snapshot.station_identifier
        transition: RadioTransition | None = None

        async with self._lock:
            connection = self._connection()
            await connection.execute(
                "INSERT OR IGNORE INTO radio_state(station_identifier) VALUES (?)",
                (station,),
            )
            cursor = await connection.execute(
                "SELECT * FROM radio_state WHERE station_identifier = ?", (station,)
            )
            row = await cursor.fetchone()

            if not snapshot.source_ok:
                await connection.execute(
                    """
                    UPDATE radio_state
                    SET last_checked_at = ?, last_error = ?
                    WHERE station_identifier = ?
                    """,
                    (checked_at, snapshot.error, station),
                )
                await connection.commit()
                return await self._get_radio_state_unlocked(station), None

            current_state = row["current_state"]
            current_key = row["current_occurrence_key"]
            current_identity = row["current_identity"]
            current_title = row["current_title"]
            current_presenter = row["current_presenter"]
            scheduled_start = row["scheduled_start"]
            scheduled_end = row["scheduled_end"]
            detected_live_at = row["detected_live_at"]
            pending_key = row["pending_occurrence_key"]
            pending_identity = row["pending_identity"]
            pending_count = row["pending_count"]
            missing_count = row["missing_count"]

            if snapshot.is_live:
                show = snapshot.current_show
                assert show is not None
                identity = show.identity
                candidate_key = show.occurrence_key
                if candidate_key is None:
                    if current_state == RADIO_LIVE and current_identity == identity:
                        candidate_key = current_key
                    elif pending_identity == identity:
                        candidate_key = pending_key
                    if candidate_key is None:
                        candidate_key = fallback_occurrence_key(
                            identity, snapshot.checked_at
                        )

                # The same DJ stays one occurrence even when the key flips, e.g. an
                # unscheduled ``live:`` key becoming the ``schedule:`` key at slot
                # start, or back again when the DJ overruns the slot.
                if (
                    current_state == RADIO_LIVE
                    and current_key
                    and current_key != candidate_key
                    and same_broadcaster(
                        current_presenter, current_identity, show.presenter, identity
                    )
                ):
                    candidate_key = current_key

                missing_count = 0
                if current_state == RADIO_LIVE and current_key == candidate_key:
                    pending_key = None
                    pending_identity = None
                    pending_count = 0
                    current_identity = identity
                    current_title = show.title
                    current_presenter = show.presenter
                    scheduled_start = (
                        _iso(show.scheduled_start) if show.scheduled_start else None
                    )
                    scheduled_end = (
                        _iso(show.scheduled_end) if show.scheduled_end else None
                    )
                    if (
                        current_title != row["current_title"]
                        or scheduled_start != row["scheduled_start"]
                        or scheduled_end != row["scheduled_end"]
                    ):
                        await connection.execute(
                            """
                            UPDATE radio_show_history
                            SET title = ?, presenter = COALESCE(?, presenter),
                                source_event_id = COALESCE(?, source_event_id),
                                scheduled_start = COALESCE(?, scheduled_start),
                                scheduled_end = COALESCE(?, scheduled_end),
                                description = COALESCE(?, description)
                            WHERE occurrence_key = ?
                            """,
                            (
                                show.title,
                                show.presenter,
                                show.source_event_id,
                                scheduled_start,
                                scheduled_end,
                                show.description,
                                current_key,
                            ),
                        )
                else:
                    if pending_key == candidate_key:
                        pending_count += 1
                    else:
                        pending_key = candidate_key
                        pending_identity = identity
                        pending_count = 1

                    if pending_count >= confirmation_checks:
                        if current_state == RADIO_LIVE and current_key:
                            await connection.execute(
                                """
                                UPDATE radio_show_history
                                SET detected_end_at = COALESCE(detected_end_at, ?)
                                WHERE occurrence_key = ?
                                """,
                                (checked_at, current_key),
                            )
                        else:
                            resumed = await self._recently_ended_radio_occurrence(
                                connection, station, show.presenter, identity,
                                snapshot.checked_at,
                            )
                            if resumed is not None:
                                candidate_key = resumed

                        current_state = RADIO_LIVE
                        current_key = candidate_key
                        current_identity = identity
                        current_title = show.title
                        current_presenter = show.presenter
                        scheduled_start = (
                            _iso(show.scheduled_start)
                            if show.scheduled_start
                            else None
                        )
                        scheduled_end = (
                            _iso(show.scheduled_end) if show.scheduled_end else None
                        )
                        detected_live_at = checked_at
                        pending_key = None
                        pending_identity = None
                        pending_count = 0
                        await connection.execute(
                            """
                            INSERT INTO radio_show_history(
                                occurrence_key, station_identifier, source_event_id,
                                title, presenter, scheduled_start, scheduled_end,
                                detected_live_at, description, artwork_url
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(occurrence_key) DO UPDATE SET
                                title = excluded.title,
                                presenter = excluded.presenter,
                                scheduled_start = excluded.scheduled_start,
                                scheduled_end = excluded.scheduled_end,
                                detected_end_at = NULL,
                                description = excluded.description,
                                artwork_url = excluded.artwork_url
                            """,
                            (
                                candidate_key,
                                station,
                                show.source_event_id,
                                show.title,
                                show.presenter,
                                scheduled_start,
                                scheduled_end,
                                checked_at,
                                show.description,
                                show.artwork_url,
                            ),
                        )
                        transition = RadioTransition(
                            kind="go_live",
                            occurrence_key=candidate_key,
                            changed_at=snapshot.checked_at,
                        )
            else:
                pending_key = None
                pending_identity = None
                pending_count = 0
                if current_state == RADIO_LIVE and current_key:
                    missing_count += 1
                    if missing_count >= confirmation_checks:
                        await connection.execute(
                            """
                            UPDATE radio_show_history
                            SET detected_end_at = COALESCE(detected_end_at, ?)
                            WHERE occurrence_key = ?
                            """,
                            (checked_at, current_key),
                        )
                        transition = RadioTransition(
                            kind="ended",
                            occurrence_key=current_key,
                            changed_at=snapshot.checked_at,
                        )
                        current_state = snapshot.state
                        current_key = None
                        current_identity = None
                        current_title = None
                        current_presenter = None
                        scheduled_start = None
                        scheduled_end = None
                        detected_live_at = None
                        missing_count = 0
                else:
                    current_state = snapshot.state
                    missing_count = 0

            await connection.execute(
                """
                UPDATE radio_state
                SET current_state = ?, current_occurrence_key = ?,
                    current_identity = ?, current_title = ?, current_presenter = ?,
                    scheduled_start = ?, scheduled_end = ?, detected_live_at = ?,
                    pending_occurrence_key = ?, pending_identity = ?,
                    pending_count = ?, missing_count = ?, last_checked_at = ?,
                    last_success_at = ?, last_error = NULL
                WHERE station_identifier = ?
                """,
                (
                    current_state,
                    current_key,
                    current_identity,
                    current_title,
                    current_presenter,
                    scheduled_start,
                    scheduled_end,
                    detected_live_at,
                    pending_key,
                    pending_identity,
                    pending_count,
                    missing_count,
                    checked_at,
                    checked_at,
                    station,
                ),
            )
            await connection.commit()
            return await self._get_radio_state_unlocked(station), transition

    async def get_radio_state(self, station_identifier: str) -> RadioState:
        async with self._lock:
            await self._connection().execute(
                "INSERT OR IGNORE INTO radio_state(station_identifier) VALUES (?)",
                (station_identifier,),
            )
            await self._connection().commit()
            return await self._get_radio_state_unlocked(station_identifier)

    async def _get_radio_state_unlocked(
        self, station_identifier: str
    ) -> RadioState:
        cursor = await self._connection().execute(
            "SELECT * FROM radio_state WHERE station_identifier = ?",
            (station_identifier,),
        )
        row = await cursor.fetchone()
        return RadioState(
            station_identifier=row["station_identifier"],
            current_state=row["current_state"],
            current_occurrence_key=row["current_occurrence_key"],
            current_identity=row["current_identity"],
            current_title=row["current_title"],
            current_presenter=row["current_presenter"],
            scheduled_start=_datetime(row["scheduled_start"]),
            scheduled_end=_datetime(row["scheduled_end"]),
            detected_live_at=_datetime(row["detected_live_at"]),
            pending_occurrence_key=row["pending_occurrence_key"],
            pending_identity=row["pending_identity"],
            pending_count=row["pending_count"],
            missing_count=row["missing_count"],
            checked_at=_datetime(row["last_checked_at"]),
            last_success_at=_datetime(row["last_success_at"]),
            error=row["last_error"],
        )

    async def _recently_ended_radio_occurrence(
        self,
        connection: aiosqlite.Connection,
        station: str,
        presenter: str | None,
        identity: str,
        now: datetime,
    ) -> str | None:
        """Return the key of a show by the same DJ that ended within the grace window."""
        cursor = await connection.execute(
            """
            SELECT occurrence_key, presenter, source_event_id, title, detected_end_at
            FROM radio_show_history
            WHERE station_identifier = ? AND detected_end_at IS NOT NULL
            ORDER BY detected_end_at DESC
            LIMIT 5
            """,
            (station,),
        )
        for row in await cursor.fetchall():
            ended_at = _datetime(row["detected_end_at"])
            if ended_at is None or now - ended_at > RADIO_RESUME_GRACE:
                continue
            previous_identity = radio_identity(
                row["source_event_id"], row["presenter"], row["title"]
            )
            if same_broadcaster(row["presenter"], previous_identity, presenter, identity):
                return str(row["occurrence_key"])
        return None

    async def announced_radio_schedule_keys(
        self, station_identifier: str, keys: Iterable[str]
    ) -> set[str]:
        wanted = [key for key in keys if key]
        if not wanted:
            return set()
        placeholders = ",".join("?" for _ in wanted)
        async with self._lock:
            cursor = await self._connection().execute(
                f"""
                SELECT occurrence_key FROM radio_schedule_announcements
                WHERE station_identifier = ? AND occurrence_key IN ({placeholders})
                """,
                (station_identifier, *wanted),
            )
            rows = await cursor.fetchall()
        return {str(row["occurrence_key"]) for row in rows}

    async def mark_radio_schedule_announced(
        self,
        station_identifier: str,
        show: RadioShow,
        announced_at: datetime,
        message_id: int | None,
    ) -> None:
        if show.occurrence_key is None or show.scheduled_start is None:
            raise ValueError("Only scheduled shows with a start time can be announced")
        async with self._lock:
            await self._connection().execute(
                """
                INSERT INTO radio_schedule_announcements(
                    occurrence_key, station_identifier, title, presenter,
                    scheduled_start, scheduled_end, announced_at, message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(occurrence_key) DO UPDATE SET
                    announced_at = excluded.announced_at,
                    message_id = excluded.message_id
                """,
                (
                    show.occurrence_key,
                    station_identifier,
                    show.title,
                    show.presenter,
                    _iso(show.scheduled_start),
                    _iso(show.scheduled_end) if show.scheduled_end else None,
                    _iso(announced_at),
                    message_id,
                ),
            )
            await self._connection().commit()

    async def pending_radio_notifications(
        self, station_identifier: str
    ) -> list[RadioOccurrence]:
        async with self._lock:
            cursor = await self._connection().execute(
                """
                SELECT * FROM radio_show_history
                WHERE station_identifier = ? AND notification_sent = 0
                  AND detected_end_at IS NULL
                ORDER BY detected_live_at ASC
                """,
                (station_identifier,),
            )
            rows = await cursor.fetchall()
        return [self._radio_occurrence_from_row(row) for row in rows]

    async def mark_radio_notification_sent(
        self, occurrence_key: str, message_id: int | None
    ) -> None:
        async with self._lock:
            await self._connection().execute(
                """
                UPDATE radio_show_history
                SET notification_sent = 1, notification_message_id = ?
                WHERE occurrence_key = ?
                """,
                (message_id, occurrence_key),
            )
            await self._connection().commit()

    async def recent_radio_occurrences(
        self, station_identifier: str, limit: int = 10
    ) -> list[RadioOccurrence]:
        async with self._lock:
            cursor = await self._connection().execute(
                """
                SELECT * FROM radio_show_history
                WHERE station_identifier = ?
                ORDER BY detected_live_at DESC LIMIT ?
                """,
                (station_identifier, limit),
            )
            rows = await cursor.fetchall()
        return [self._radio_occurrence_from_row(row) for row in rows]

    @staticmethod
    def _radio_occurrence_from_row(row: aiosqlite.Row) -> RadioOccurrence:
        detected_live_at = _datetime(row["detected_live_at"])
        assert detected_live_at is not None
        return RadioOccurrence(
            occurrence_key=row["occurrence_key"],
            station_identifier=row["station_identifier"],
            source_event_id=row["source_event_id"],
            title=row["title"],
            presenter=row["presenter"],
            scheduled_start=_datetime(row["scheduled_start"]),
            scheduled_end=_datetime(row["scheduled_end"]),
            detected_live_at=detected_live_at,
            detected_end_at=_datetime(row["detected_end_at"]),
            description=row["description"],
            artwork_url=row["artwork_url"],
            notification_sent=bool(row["notification_sent"]),
            notification_message_id=row["notification_message_id"],
        )

    async def submit_report(
        self,
        guild_id: int,
        user_id: int,
        realm: str,
        issue: str,
        details: str | None,
        active_minutes: int,
        *,
        now: datetime | None = None,
    ) -> tuple[CommunityReport, bool]:
        details = validate_report(realm, issue, details)
        current = now or datetime.now(timezone.utc)
        cutoff = current - timedelta(minutes=active_minutes)
        async with self._lock:
            connection = self._connection()
            cursor = await connection.execute(
                """
                SELECT id FROM community_reports
                WHERE guild_id = ? AND user_id = ? AND realm = ? AND issue = ?
                  AND cleared_at IS NULL AND updated_at >= ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (guild_id, user_id, realm, issue, _iso(cutoff)),
            )
            existing = await cursor.fetchone()
            refreshed = existing is not None
            if existing:
                await connection.execute(
                    """
                    UPDATE community_reports
                    SET details = COALESCE(?, details), updated_at = ?
                    WHERE id = ?
                    """,
                    (details, _iso(current), existing["id"]),
                )
                report_id = existing["id"]
            else:
                cursor = await connection.execute(
                    """
                    INSERT INTO community_reports(
                        guild_id, user_id, realm, issue, details, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        guild_id,
                        user_id,
                        realm,
                        issue,
                        details,
                        _iso(current),
                        _iso(current),
                    ),
                )
                report_id = cursor.lastrowid
            await connection.commit()
            cursor = await connection.execute(
                "SELECT * FROM community_reports WHERE id = ?", (report_id,)
            )
            row = await cursor.fetchone()
        return self._report_from_row(row), refreshed

    async def active_reports(
        self,
        guild_id: int,
        active_minutes: int,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[CommunityReport]:
        """Return current non-cleared reports newest-first for moderator detail views."""
        current = now or datetime.now(timezone.utc)
        cutoff = current - timedelta(minutes=active_minutes)
        async with self._lock:
            cursor = await self._connection().execute(
                """
                SELECT * FROM community_reports
                WHERE guild_id = ? AND cleared_at IS NULL AND updated_at >= ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (guild_id, _iso(cutoff), max(1, limit)),
            )
            rows = await cursor.fetchall()
        return [self._report_from_row(row) for row in rows]

    async def report_summary(
        self,
        guild_id: int,
        active_minutes: int,
        threshold: int,
        *,
        now: datetime | None = None,
    ) -> ReportSummary:
        current = now or datetime.now(timezone.utc)
        cutoff = current - timedelta(minutes=active_minutes)
        async with self._lock:
            connection = self._connection()
            cursor = await connection.execute(
                """
                SELECT realm, issue, COUNT(*) AS report_count,
                       COUNT(DISTINCT user_id) AS unique_users,
                       MAX(updated_at) AS latest_at
                FROM community_reports
                WHERE guild_id = ? AND cleared_at IS NULL AND updated_at >= ?
                GROUP BY realm, issue
                ORDER BY realm, issue
                """,
                (guild_id, _iso(cutoff)),
            )
            rows = await cursor.fetchall()
            cursor = await connection.execute(
                """
                SELECT COUNT(*) AS total_reports,
                       COUNT(DISTINCT user_id) AS unique_users
                FROM community_reports
                WHERE guild_id = ? AND cleared_at IS NULL AND updated_at >= ?
                """,
                (guild_id, _iso(cutoff)),
            )
            totals = await cursor.fetchone()

        groups = tuple(
            ReportGroup(
                realm=row["realm"],
                issue=row["issue"],
                report_count=row["report_count"],
                unique_users=row["unique_users"],
                latest_at=_datetime(row["latest_at"]),
            )
            for row in rows
        )
        return ReportSummary(
            groups=groups,
            unique_users=totals["unique_users"],
            total_reports=totals["total_reports"],
            active_minutes=active_minutes,
            threshold=threshold,
        )

    async def apply_report_alert_state(
        self,
        guild_id: int,
        summary: ReportSummary,
        recovery_confirmations: int = 2,
        *,
        now: datetime | None = None,
    ) -> None:
        if recovery_confirmations <= 0:
            raise ValueError("Report recovery confirmation count must be positive")
        current = now or datetime.now(timezone.utc)
        checked_at = _iso(current)
        async with self._lock:
            connection = self._connection()
            await connection.execute(
                "INSERT OR IGNORE INTO report_alert_state(guild_id) VALUES (?)",
                (guild_id,),
            )
            cursor = await connection.execute(
                "SELECT * FROM report_alert_state WHERE guild_id = ?", (guild_id,)
            )
            row = await cursor.fetchone()
            degraded = bool(row["confirmed_degraded"])
            below_count = row["below_threshold_count"]
            incident_id = row["current_incident_id"]

            if summary.degraded:
                below_count = 0
                if not degraded or incident_id is None:
                    cursor = await connection.execute(
                        """
                        INSERT INTO report_incidents(
                            guild_id, started_at, peak_unique_users,
                            latest_unique_users
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            guild_id,
                            checked_at,
                            summary.unique_users,
                            summary.unique_users,
                        ),
                    )
                    incident_id = cursor.lastrowid
                    degraded = True
                else:
                    await connection.execute(
                        """
                        UPDATE report_incidents
                        SET peak_unique_users = MAX(peak_unique_users, ?),
                            latest_unique_users = ?
                        WHERE id = ?
                        """,
                        (summary.unique_users, summary.unique_users, incident_id),
                    )
            elif degraded and incident_id is not None:
                below_count += 1
                await connection.execute(
                    "UPDATE report_incidents SET latest_unique_users = ? WHERE id = ?",
                    (summary.unique_users, incident_id),
                )
                if below_count >= recovery_confirmations:
                    await connection.execute(
                        "UPDATE report_incidents SET ended_at = ? WHERE id = ?",
                        (checked_at, incident_id),
                    )
                    degraded = False
                    below_count = 0
                    incident_id = None
            else:
                below_count = 0

            await connection.execute(
                """
                UPDATE report_alert_state
                SET confirmed_degraded = ?, below_threshold_count = ?,
                    current_incident_id = ?, last_checked_at = ?
                WHERE guild_id = ?
                """,
                (degraded, below_count, incident_id, checked_at, guild_id),
            )
            await connection.commit()

    async def pending_report_incident_alerts(
        self, guild_id: int
    ) -> list[ReportIncidentNotification]:
        async with self._lock:
            cursor = await self._connection().execute(
                """
                SELECT * FROM report_incidents
                WHERE guild_id = ?
                  AND (alert_sent = 0
                    OR (ended_at IS NOT NULL AND recovery_sent = 0))
                ORDER BY started_at ASC
                """,
                (guild_id,),
            )
            rows = await cursor.fetchall()

        notifications: list[ReportIncidentNotification] = []
        for row in rows:
            incident = self._report_incident_from_row(row)
            if not row["alert_sent"]:
                notifications.append(ReportIncidentNotification("degraded", incident))
            if incident.ended_at is not None and not row["recovery_sent"]:
                notifications.append(ReportIncidentNotification("recovery", incident))
        return notifications

    async def mark_report_incident_alert_sent(
        self, incident_id: int, kind: str
    ) -> None:
        if kind not in {"degraded", "recovery"}:
            raise ValueError("Unknown report incident alert kind")
        column = "alert_sent" if kind == "degraded" else "recovery_sent"
        async with self._lock:
            await self._connection().execute(
                f"UPDATE report_incidents SET {column} = 1 WHERE id = ?",
                (incident_id,),
            )
            await self._connection().commit()

    async def recent_report_incidents(
        self, guild_id: int, limit: int = 10
    ) -> list[ReportIncident]:
        async with self._lock:
            cursor = await self._connection().execute(
                """
                SELECT * FROM report_incidents
                WHERE guild_id = ? ORDER BY started_at DESC LIMIT ?
                """,
                (guild_id, limit),
            )
            rows = await cursor.fetchall()
        return [self._report_incident_from_row(row) for row in rows]

    @staticmethod
    def _report_incident_from_row(row: aiosqlite.Row) -> ReportIncident:
        started_at = _datetime(row["started_at"])
        assert started_at is not None
        return ReportIncident(
            incident_id=row["id"],
            guild_id=row["guild_id"],
            started_at=started_at,
            ended_at=_datetime(row["ended_at"]),
            peak_unique_users=row["peak_unique_users"],
            latest_unique_users=row["latest_unique_users"],
        )

    async def clear_reports(
        self,
        guild_id: int,
        cleared_by_user_id: int,
        active_minutes: int,
        *,
        now: datetime | None = None,
    ) -> int:
        current = now or datetime.now(timezone.utc)
        cutoff = current - timedelta(minutes=active_minutes)
        async with self._lock:
            cursor = await self._connection().execute(
                """
                UPDATE community_reports
                SET cleared_at = ?, cleared_by_user_id = ?
                WHERE guild_id = ? AND cleared_at IS NULL AND updated_at >= ?
                """,
                (_iso(current), cleared_by_user_id, guild_id, _iso(cutoff)),
            )
            await self._connection().commit()
            return cursor.rowcount

    @staticmethod
    def _report_from_row(row: aiosqlite.Row) -> CommunityReport:
        created_at = _datetime(row["created_at"])
        updated_at = _datetime(row["updated_at"])
        assert created_at is not None and updated_at is not None
        return CommunityReport(
            report_id=row["id"],
            guild_id=row["guild_id"],
            user_id=row["user_id"],
            realm=row["realm"],
            issue=row["issue"],
            details=row["details"],
            created_at=created_at,
            updated_at=updated_at,
        )

    async def command_roles(self, guild_id: int, command_name: str) -> set[int]:
        if command_name not in MANAGED_COMMANDS:
            raise ValueError("Unknown managed command")
        async with self._lock:
            cursor = await self._connection().execute(
                """
                SELECT role_id FROM command_roles
                WHERE guild_id = ? AND command_name = ?
                """,
                (guild_id, command_name),
            )
            return {row["role_id"] for row in await cursor.fetchall()}

    async def all_command_roles(self, guild_id: int) -> dict[str, set[int]]:
        result = {command: set() for command in MANAGED_COMMANDS}
        async with self._lock:
            cursor = await self._connection().execute(
                "SELECT command_name, role_id FROM command_roles WHERE guild_id = ?",
                (guild_id,),
            )
            for row in await cursor.fetchall():
                if row["command_name"] in result:
                    result[row["command_name"]].add(row["role_id"])
        return result

    async def add_command_role(
        self,
        guild_id: int,
        command_name: str,
        role_id: int,
        created_by_user_id: int,
    ) -> bool:
        if command_name not in MANAGED_COMMANDS:
            raise ValueError("Unknown managed command")
        async with self._lock:
            cursor = await self._connection().execute(
                """
                INSERT OR IGNORE INTO command_roles(
                    guild_id, command_name, role_id, created_at, created_by_user_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    command_name,
                    role_id,
                    _iso(datetime.now(timezone.utc)),
                    created_by_user_id,
                ),
            )
            await self._connection().commit()
            return cursor.rowcount > 0

    async def remove_command_role(
        self, guild_id: int, command_name: str, role_id: int
    ) -> bool:
        if command_name not in MANAGED_COMMANDS:
            raise ValueError("Unknown managed command")
        async with self._lock:
            cursor = await self._connection().execute(
                """
                DELETE FROM command_roles
                WHERE guild_id = ? AND command_name = ? AND role_id = ?
                """,
                (guild_id, command_name, role_id),
            )
            await self._connection().commit()
            return cursor.rowcount > 0

    async def uptime_stats(self, window: timedelta) -> UptimeStats:
        now = datetime.now(timezone.utc)
        window_start = now - window
        async with self._lock:
            cursor = await self._connection().execute(
                """
                SELECT status, started_at, ended_at FROM service_history
                WHERE started_at < ? AND (ended_at IS NULL OR ended_at > ?)
                """,
                (_iso(now), _iso(window_start)),
            )
            rows = await cursor.fetchall()

        online_seconds = 0.0
        observed_seconds = 0.0
        for row in rows:
            if row["status"] not in {ONLINE, PARTIAL, OFFLINE}:
                continue
            start = max(_datetime(row["started_at"]), window_start)
            end_value = _datetime(row["ended_at"])
            end = min(end_value or now, now)
            seconds = max(0.0, (end - start).total_seconds())
            observed_seconds += seconds
            if row["status"] == ONLINE:
                online_seconds += seconds

        return UptimeStats(
            window_seconds=int(window.total_seconds()),
            online_seconds=online_seconds,
            observed_seconds=observed_seconds,
        )

    async def recent_incidents(self, limit: int = 10) -> list[Incident]:
        async with self._lock:
            cursor = await self._connection().execute(
                """
                SELECT * FROM incidents ORDER BY started_at DESC LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [
            self._incident_from_row(row)
            for row in rows
        ]

    async def pending_incident_alerts(self) -> list[IncidentNotification]:
        async with self._lock:
            cursor = await self._connection().execute(
                """
                SELECT * FROM incidents
                WHERE outage_alert_sent = 0
                   OR (ended_at IS NOT NULL AND recovery_alert_sent = 0)
                ORDER BY started_at ASC
                """
            )
            rows = await cursor.fetchall()

        notifications: list[IncidentNotification] = []
        for row in rows:
            incident = self._incident_from_row(row)
            if not row["outage_alert_sent"]:
                notifications.append(IncidentNotification("outage", incident))
            if incident.ended_at is not None and not row["recovery_alert_sent"]:
                notifications.append(IncidentNotification("recovery", incident))
        return notifications

    async def mark_incident_alert_sent(self, incident_id: int, kind: str) -> None:
        if kind not in {"outage", "recovery"}:
            raise ValueError("Unknown incident alert kind")
        column = "outage_alert_sent" if kind == "outage" else "recovery_alert_sent"
        async with self._lock:
            await self._connection().execute(
                f"UPDATE incidents SET {column} = 1 WHERE id = ?",
                (incident_id,),
            )
            await self._connection().commit()

    @staticmethod
    def _incident_from_row(row: aiosqlite.Row) -> Incident:
        started_at = _datetime(row["started_at"])
        assert started_at is not None
        return Incident(
            incident_id=row["id"],
            started_at=started_at,
            ended_at=_datetime(row["ended_at"]),
            initial_status=row["initial_status"],
            worst_status=row["worst_status"],
        )

    async def recent_auth_incidents(self, limit: int = 10) -> list[AuthIncident]:
        async with self._lock:
            cursor = await self._connection().execute(
                "SELECT * FROM auth_incidents ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
        return [self._auth_incident_from_row(row) for row in rows]

    async def pending_auth_incident_alerts(
        self,
    ) -> list[AuthIncidentNotification]:
        async with self._lock:
            cursor = await self._connection().execute(
                """
                SELECT * FROM auth_incidents
                WHERE degraded_alert_sent = 0
                   OR (ended_at IS NOT NULL AND recovery_alert_sent = 0)
                ORDER BY started_at ASC
                """
            )
            rows = await cursor.fetchall()

        notifications: list[AuthIncidentNotification] = []
        for row in rows:
            incident = self._auth_incident_from_row(row)
            if not row["degraded_alert_sent"]:
                notifications.append(AuthIncidentNotification("degraded", incident))
            if incident.ended_at is not None and not row["recovery_alert_sent"]:
                notifications.append(AuthIncidentNotification("recovery", incident))
        return notifications

    async def mark_auth_incident_alert_sent(
        self, incident_id: int, kind: str
    ) -> None:
        if kind not in {"degraded", "recovery"}:
            raise ValueError("Unknown authentication incident alert kind")
        column = (
            "degraded_alert_sent" if kind == "degraded" else "recovery_alert_sent"
        )
        async with self._lock:
            await self._connection().execute(
                f"UPDATE auth_incidents SET {column} = 1 WHERE id = ?",
                (incident_id,),
            )
            await self._connection().commit()

    @staticmethod
    def _auth_incident_from_row(row: aiosqlite.Row) -> AuthIncident:
        started_at = _datetime(row["started_at"])
        assert started_at is not None
        return AuthIncident(
            incident_id=row["id"],
            started_at=started_at,
            ended_at=_datetime(row["ended_at"]),
            initial_status=row["initial_status"],
            latest_status=row["latest_status"],
            tcp_status=row["tcp_status"],
            reason=row["reason"],
            latency_ms=row["last_latency_ms"],
        )

    async def announcements_initialized(self) -> bool:
        return await self.get_metadata("announcements_initialized") == "1"

    async def seed_announcements(self, announcements: list[Announcement]) -> None:
        async with self._lock:
            connection = self._connection()
            discovered_at = _iso(datetime.now(timezone.utc))
            for item in announcements:
                await connection.execute(
                    """
                    INSERT OR IGNORE INTO announcements(
                        topic_id, title, author, published_at, preview, body,
                        content_source, url, discovered_at, announced,
                        delivery_pages_sent
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
                    """,
                    (
                        item.topic_id,
                        item.title,
                        item.author,
                        item.published_at,
                        item.preview,
                        item.body,
                        item.content_source,
                        item.url,
                        discovered_at,
                    ),
                )
            await connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES ('announcements_initialized', '1')
                ON CONFLICT(key) DO UPDATE SET value = '1'
                """
            )
            await connection.commit()

    async def store_announcement(
        self, announcement: Announcement, *, announced: bool = False
    ) -> None:
        async with self._lock:
            await self._connection().execute(
                """
                INSERT INTO announcements(
                    topic_id, title, author, published_at, preview, body,
                    content_source, url, discovered_at, announced,
                    delivery_pages_sent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(topic_id) DO UPDATE SET
                    title = excluded.title,
                    author = COALESCE(excluded.author, announcements.author),
                    published_at = COALESCE(
                        excluded.published_at, announcements.published_at
                    ),
                    preview = COALESCE(excluded.preview, announcements.preview),
                    body = COALESCE(excluded.body, announcements.body),
                    content_source = CASE
                        WHEN excluded.body IS NOT NULL THEN excluded.content_source
                        ELSE COALESCE(
                            announcements.content_source, excluded.content_source
                        )
                    END,
                    url = excluded.url,
                    announced = CASE
                        WHEN excluded.announced = 1 THEN 1
                        ELSE announcements.announced
                    END
                """,
                (
                    announcement.topic_id,
                    announcement.title,
                    announcement.author,
                    announcement.published_at,
                    announcement.preview,
                    announcement.body,
                    announcement.content_source,
                    announcement.url,
                    _iso(datetime.now(timezone.utc)),
                    int(announced),
                ),
            )
            await self._connection().commit()

    async def known_topic_ids(self) -> set[int]:
        async with self._lock:
            cursor = await self._connection().execute(
                "SELECT topic_id FROM announcements"
            )
            return {row["topic_id"] for row in await cursor.fetchall()}

    async def pending_announcements(self) -> list[Announcement]:
        async with self._lock:
            cursor = await self._connection().execute(
                """
                SELECT * FROM announcements WHERE announced = 0
                ORDER BY topic_id ASC
                """
            )
            rows = await cursor.fetchall()
        return [self._announcement_from_row(row) for row in rows]

    async def mark_announcement_sent(self, topic_id: int) -> None:
        async with self._lock:
            await self._connection().execute(
                "UPDATE announcements SET announced = 1 WHERE topic_id = ?",
                (topic_id,),
            )
            await self._connection().commit()

    async def announcement_delivery_progress(self, topic_id: int) -> int:
        async with self._lock:
            cursor = await self._connection().execute(
                "SELECT delivery_pages_sent FROM announcements WHERE topic_id = ?",
                (topic_id,),
            )
            row = await cursor.fetchone()
            return row["delivery_pages_sent"] if row else 0

    async def mark_announcement_page_sent(
        self, topic_id: int, pages_sent: int
    ) -> None:
        async with self._lock:
            await self._connection().execute(
                """
                UPDATE announcements
                SET delivery_pages_sent = MAX(delivery_pages_sent, ?)
                WHERE topic_id = ?
                """,
                (pages_sent, topic_id),
            )
            await self._connection().commit()

    async def latest_announcement(self) -> Announcement | None:
        async with self._lock:
            cursor = await self._connection().execute(
                "SELECT * FROM announcements ORDER BY topic_id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
        return self._announcement_from_row(row) if row else None

    @staticmethod
    def _announcement_from_row(row: aiosqlite.Row) -> Announcement:
        return Announcement(
            topic_id=row["topic_id"],
            title=row["title"],
            author=row["author"],
            published_at=row["published_at"],
            preview=row["preview"],
            body=row["body"],
            content_source=row["content_source"],
            url=row["url"],
        )
