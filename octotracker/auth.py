from __future__ import annotations

import asyncio
import logging
import struct
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter

from .connectivity import (
    REACHABLE,
    UNKNOWN as TCP_UNKNOWN,
    UNREACHABLE,
    ConnectivityResult,
)


LOGGER = logging.getLogger(__name__)

RESPONSIVE = "responsive"
UNRESPONSIVE = "unresponsive"
MALFORMED_RESPONSE = "malformed_response"
UNKNOWN = "unknown"

AUTH_LOGON_CHALLENGE = 0x00
VANILLA_PROTOCOL_VERSION = 3
VANILLA_BUILD = 5875
LOGIN_RESULT_CODES = frozenset(range(0x10))
SYNTHETIC_ACCOUNT = "OCTOTRACKERPROBE"


@dataclass(frozen=True, slots=True)
class AuthProbeResult:
    checked_at: datetime
    tcp_status: str
    auth_status: str
    host: str
    port: int | None
    latency_ms: int | None
    reason: str | None = None
    result_code: int | None = None

    def connectivity_result(self) -> ConnectivityResult:
        return ConnectivityResult(
            checked_at=self.checked_at,
            status=self.tcp_status,
            host=self.host,
            port=self.port,
            error=self.reason if self.tcp_status != REACHABLE else None,
        )


@dataclass(frozen=True, slots=True)
class AuthState:
    status: str
    status_since: datetime | None
    pending_status: str | None
    pending_count: int
    observed_status: str
    tcp_status: str
    checked_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    latency_ms: int | None
    consecutive_successes: int
    consecutive_failures: int
    reason: str | None
    host: str
    port: int | None


@dataclass(frozen=True, slots=True)
class AuthTransition:
    kind: str | None
    old_status: str
    new_status: str
    changed_at: datetime
    incident_id: int | None = None
    duration_seconds: float | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_logon_challenge(
    account_name: str = SYNTHETIC_ACCOUNT,
    *,
    timezone_offset_minutes: int = 0,
    client_ip: bytes = b"\x7f\x00\x00\x01",
) -> bytes:
    """Build a WoW 1.12.1 protocol-3 logon challenge without credentials."""
    try:
        account = account_name.upper().encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("The synthetic account name must be ASCII") from exc
    if not 1 <= len(account) <= 16:
        raise ValueError("The synthetic account name must contain 1 to 16 bytes")
    if len(client_ip) != 4:
        raise ValueError("client_ip must contain exactly four bytes")

    body = b"".join(
        (
            b"WoW\x00",
            bytes((1, 12, 1)),
            struct.pack("<H", VANILLA_BUILD),
            b"68x\x00",  # x86, as encoded by the original little-endian client.
            b"niW\x00",  # Win, as encoded by the original little-endian client.
            b"BGne",  # enGB, as encoded by the original little-endian client.
            struct.pack("<i", timezone_offset_minutes),
            client_ip,
            bytes((len(account),)),
            account,
        )
    )
    return bytes((AUTH_LOGON_CHALLENGE, VANILLA_PROTOCOL_VERSION)) + struct.pack(
        "<H", len(body)
    ) + body


async def _validate_server_response(reader: asyncio.StreamReader) -> int:
    prefix = await reader.readexactly(3)
    opcode, protocol, result_code = prefix
    if opcode != AUTH_LOGON_CHALLENGE:
        raise ValueError("Unexpected authentication opcode")
    if protocol != 0:
        raise ValueError("Unexpected authentication response version")
    if result_code not in LOGIN_RESULT_CODES:
        raise ValueError("Unknown authentication result code")

    # Any defined failure result is already a complete, protocol-valid reply.
    # A success result has a variable-length SRP challenge that must be framed
    # correctly before it can be considered valid. We never send an SRP proof.
    if result_code != 0:
        return result_code

    await reader.readexactly(32)  # SRP public value B
    generator_length = (await reader.readexactly(1))[0]
    if not 1 <= generator_length <= 32:
        raise ValueError("Invalid SRP generator length")
    await reader.readexactly(generator_length)

    prime_length = (await reader.readexactly(1))[0]
    if not 1 <= prime_length <= 32:
        raise ValueError("Invalid SRP prime length")
    await reader.readexactly(prime_length)
    await reader.readexactly(32)  # Salt
    await reader.readexactly(16)  # CRC salt

    security_flag = (await reader.readexactly(1))[0]
    if security_flag not in {0, 1}:
        raise ValueError("Unknown security flag")
    if security_flag == 1:
        await reader.readexactly(20)  # PIN grid seed and salt
    return result_code


class AuthProbe:
    """A serialized, credential-free health probe for the Vanilla login endpoint."""

    def __init__(self, host: str, port: int | None, timeout_seconds: int):
        self.host = host
        self.port = port
        self.timeout_seconds = timeout_seconds
        self._lock = asyncio.Lock()

    async def probe(self) -> AuthProbeResult:
        async with self._lock:
            return await self._probe_once()

    async def _probe_once(self) -> AuthProbeResult:
        checked_at = utc_now()
        if self.port is None:
            return AuthProbeResult(
                checked_at=checked_at,
                tcp_status=TCP_UNKNOWN,
                auth_status=UNKNOWN,
                host=self.host,
                port=None,
                latency_ms=None,
                reason="No authentication port has been configured",
            )

        writer: asyncio.StreamWriter | None = None
        connected = False
        response_started = False
        started = perf_counter()
        LOGGER.debug("Authentication health probe starting for %s:%s", self.host, self.port)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                reader, writer = await asyncio.open_connection(self.host, self.port)
                connected = True
                writer.write(build_logon_challenge())
                await writer.drain()
                LOGGER.debug("Authentication challenge sent; awaiting framed response")
                try:
                    result_code = await _validate_server_response(reader)
                except asyncio.IncompleteReadError as exc:
                    response_started = bool(exc.partial)
                    raise

            latency_ms = max(0, round((perf_counter() - started) * 1000))
            LOGGER.debug("Authentication endpoint returned a valid protocol response")
            return AuthProbeResult(
                checked_at=checked_at,
                tcp_status=REACHABLE,
                auth_status=RESPONSIVE,
                host=self.host,
                port=self.port,
                latency_ms=latency_ms,
                reason=f"Valid logon challenge response (result {result_code})",
                result_code=result_code,
            )
        except ValueError as exc:
            return AuthProbeResult(
                checked_at=checked_at,
                tcp_status=REACHABLE,
                auth_status=MALFORMED_RESPONSE,
                host=self.host,
                port=self.port,
                latency_ms=max(0, round((perf_counter() - started) * 1000)),
                reason=str(exc),
            )
        except asyncio.IncompleteReadError as exc:
            malformed = response_started or bool(exc.partial)
            return AuthProbeResult(
                checked_at=checked_at,
                tcp_status=REACHABLE,
                auth_status=MALFORMED_RESPONSE if malformed else UNRESPONSIVE,
                host=self.host,
                port=self.port,
                latency_ms=max(0, round((perf_counter() - started) * 1000)),
                reason=(
                    "Authentication server closed a partial response"
                    if malformed
                    else "Authentication server closed without a response"
                ),
            )
        except (TimeoutError, asyncio.TimeoutError):
            return AuthProbeResult(
                checked_at=checked_at,
                tcp_status=REACHABLE if connected else UNREACHABLE,
                auth_status=UNRESPONSIVE if connected else UNKNOWN,
                host=self.host,
                port=self.port,
                latency_ms=max(0, round((perf_counter() - started) * 1000)),
                reason=(
                    "Authentication response timed out"
                    if connected
                    else "TCP connection timed out"
                ),
            )
        except OSError:
            return AuthProbeResult(
                checked_at=checked_at,
                tcp_status=REACHABLE if connected else UNREACHABLE,
                auth_status=UNRESPONSIVE if connected else UNKNOWN,
                host=self.host,
                port=self.port,
                latency_ms=max(0, round((perf_counter() - started) * 1000)),
                reason=(
                    "Connection failed after the TCP session opened"
                    if connected
                    else "TCP connection failed"
                ),
            )
        finally:
            if writer is not None:
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
            LOGGER.debug("Authentication health probe connection closed")
