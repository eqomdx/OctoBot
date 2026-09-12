from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone


REACHABLE = "reachable"
UNREACHABLE = "unreachable"
UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ConnectivityResult:
    checked_at: datetime
    status: str
    host: str
    port: int | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectivityState:
    status: str
    status_since: datetime | None
    pending_status: str | None
    pending_count: int
    checked_at: datetime | None
    last_success_at: datetime | None
    error: str | None
    host: str
    port: int | None


class ConnectivityProbe:
    def __init__(self, host: str, port: int | None, timeout_seconds: int):
        self.host = host
        self.port = port
        self.timeout_seconds = timeout_seconds

    async def probe(self) -> ConnectivityResult:
        checked_at = datetime.now(timezone.utc)
        if self.port is None:
            return ConnectivityResult(
                checked_at=checked_at,
                status=UNKNOWN,
                host=self.host,
                port=None,
                error="No authoritative game port has been configured",
            )

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout_seconds,
            )
            del reader
            writer.close()
            await writer.wait_closed()
        except (OSError, TimeoutError, asyncio.TimeoutError) as exc:
            return ConnectivityResult(
                checked_at=checked_at,
                status=UNREACHABLE,
                host=self.host,
                port=self.port,
                error=f"TCP connection failed: {type(exc).__name__}",
            )

        return ConnectivityResult(
            checked_at=checked_at,
            status=REACHABLE,
            host=self.host,
            port=self.port,
        )
