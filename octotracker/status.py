from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp
from bs4 import BeautifulSoup

from .auth import MALFORMED_RESPONSE, UNRESPONSIVE, AuthState
from .connectivity import UNREACHABLE, ConnectivityState
from .reports import ReportSummary


LOGGER = logging.getLogger(__name__)

STATUS_URL = "https://octowow.st/"
REALMS = (
    "C'Thun (Hardcore)",
    "N'Zoth (Normal)",
    "Y'Shaarj (PvP)",
)

ONLINE = "online"
OFFLINE = "offline"
UNKNOWN = "unknown"
PARTIAL = "partial"
DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class StatusResult:
    checked_at: datetime
    realms: dict[str, str]
    reachable: bool
    error: str | None = None

    @property
    def complete(self) -> bool:
        return self.reachable and all(
            status in {ONLINE, OFFLINE} for status in self.realms.values()
        )


@dataclass(frozen=True, slots=True)
class RealmState:
    name: str
    status: str
    status_since: datetime | None
    pending_status: str | None
    pending_count: int
    last_observed_status: str


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    overall_status: str
    status_since: datetime | None
    realms: tuple[RealmState, ...]
    checked_at: datetime | None
    last_success_at: datetime | None
    source_ok: bool
    source_error: str | None


@dataclass(frozen=True, slots=True)
class CombinedStatusSnapshot:
    official: StatusSnapshot
    connectivity: ConnectivityState
    reports: ReportSummary
    overall_status: str
    authentication: AuthState | None = None


@dataclass(frozen=True, slots=True)
class ServiceTransition:
    kind: str | None
    old_status: str
    new_status: str
    changed_at: datetime
    incident_id: int | None = None
    duration_seconds: float | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def overall_status(realm_statuses: dict[str, str]) -> str:
    statuses = [realm_statuses.get(realm, UNKNOWN) for realm in REALMS]
    if any(status == UNKNOWN for status in statuses):
        return UNKNOWN
    if all(status == ONLINE for status in statuses):
        return ONLINE
    if all(status == OFFLINE for status in statuses):
        return OFFLINE
    return PARTIAL


def combined_overall_status(
    official: StatusSnapshot,
    connectivity: ConnectivityState,
    reports: ReportSummary,
    authentication: AuthState | None = None,
) -> str:
    """Combine distinct signals without allowing reports to declare an outage."""
    if official.overall_status == OFFLINE:
        return OFFLINE
    if official.overall_status == PARTIAL:
        return PARTIAL
    if (
        connectivity.status == UNREACHABLE
        or (
            authentication is not None
            and authentication.status in {UNRESPONSIVE, MALFORMED_RESPONSE}
        )
        or reports.degraded
    ):
        return DEGRADED
    if official.overall_status == UNKNOWN or not official.source_ok:
        return UNKNOWN
    return ONLINE


def combine_status(
    official: StatusSnapshot,
    connectivity: ConnectivityState,
    reports: ReportSummary,
    authentication: AuthState | None = None,
) -> CombinedStatusSnapshot:
    return CombinedStatusSnapshot(
        official=official,
        connectivity=connectivity,
        reports=reports,
        overall_status=combined_overall_status(
            official, connectivity, reports, authentication
        ),
        authentication=authentication,
    )


def parse_realm_statuses(html: str) -> dict[str, str]:
    """Extract explicit realm status labels without guessing from HTTP health."""
    soup = BeautifulSoup(html, "html.parser")
    parsed = {realm: UNKNOWN for realm in REALMS}

    # Current OctoWoW markup separates the realm type into a child span, so the
    # row text is more stable than looking for one exact text node.
    for row in soup.select(".realm-row"):
        name_tag = row.select_one(".realm-name")
        status_tag = row.select_one(".realm-status")
        if name_tag is None or status_tag is None:
            continue
        name_text = re.sub(r"\s+", " ", name_tag.get_text(" ", strip=True))
        state_text = status_tag.get_text(" ", strip=True).casefold()
        state = state_text if state_text in {ONLINE, OFFLINE} else UNKNOWN
        for realm in REALMS:
            if realm.casefold() in name_text.casefold():
                parsed[realm] = state
                break

    if all(state != UNKNOWN for state in parsed.values()):
        return parsed

    strings = [re.sub(r"\s+", " ", item).strip() for item in soup.stripped_strings]
    folded = [item.casefold() for item in strings]

    for realm in REALMS:
        if parsed[realm] != UNKNOWN:
            continue
        realm_key = realm.casefold()
        status = UNKNOWN

        for index, value in enumerate(folded):
            if realm_key not in value:
                continue

            # On the live page the explicit status follows the realm label. Keep
            # the window small so unrelated marketing copy cannot be mistaken for it.
            for candidate in folded[index + 1 : index + 7]:
                candidate = candidate.strip(" ?:-")
                if candidate in {ONLINE, OFFLINE}:
                    status = candidate
                    break
            if status != UNKNOWN:
                break

        parsed[realm] = status

    return parsed


class StatusClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def fetch(self) -> StatusResult:
        checked_at = utc_now()
        try:
            async with self.session.get(STATUS_URL) as response:
                response.raise_for_status()
                html = await response.text()
        except (aiohttp.ClientError, TimeoutError) as exc:
            LOGGER.warning("OctoWoW status request failed: %s", exc)
            return StatusResult(
                checked_at=checked_at,
                realms={realm: UNKNOWN for realm in REALMS},
                reachable=False,
                error=f"Request failed: {type(exc).__name__}",
            )

        try:
            realms = parse_realm_statuses(html)
        except Exception as exc:  # BeautifulSoup/parser failures must not stop the bot.
            LOGGER.exception("Could not parse the OctoWoW status page")
            return StatusResult(
                checked_at=checked_at,
                realms={realm: UNKNOWN for realm in REALMS},
                reachable=True,
                error=f"Parse failed: {type(exc).__name__}",
            )

        lowered_html = html.casefold()
        if (
            "verifying your browser" in lowered_html
            or "<title>just a moment please" in lowered_html
        ):
            LOGGER.warning("OctoWoW returned a browser-verification page")
            return StatusResult(
                checked_at=checked_at,
                realms=realms,
                reachable=True,
                error="Website returned browser verification instead of realm status",
            )

        missing = [realm for realm, state in realms.items() if state == UNKNOWN]
        error = None
        if missing:
            error = "Could not confidently parse: " + ", ".join(missing)
            LOGGER.warning(error)

        return StatusResult(
            checked_at=checked_at,
            realms=realms,
            reachable=True,
            error=error,
        )
