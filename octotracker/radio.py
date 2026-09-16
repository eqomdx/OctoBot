from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import aiohttp


LOGGER = logging.getLogger(__name__)

# How far ahead the schedule is read. AzuraCast only expands recurring shows
# (e.g. "every Wednesday 19:00") when asked for a start/end date range; the
# ``rows`` parameter alone returns a single occurrence.
DEFAULT_SCHEDULE_DAYS = 14

LIVE = "live"
AUTODJ = "autodj"
OFFLINE = "offline"
UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RadioShow:
    occurrence_key: str | None
    source_event_id: str | None
    source_type: str
    title: str
    presenter: str | None
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    description: str | None = None
    artwork_url: str | None = None

    @property
    def identity(self) -> str:
        return radio_identity(self.source_event_id, self.presenter, self.title)


@dataclass(frozen=True, slots=True)
class RadioSnapshot:
    checked_at: datetime
    source_ok: bool
    station_identifier: str
    station_name: str
    public_url: str
    state: str
    current_track: str | None
    listeners: int | None
    current_show: RadioShow | None
    upcoming: tuple[RadioShow, ...]
    schedule_available: bool
    station_timezone: str | None
    platform_confirmed: bool
    error: str | None = None
    schedule_error: str | None = None
    schedule: tuple[RadioShow, ...] = ()

    @property
    def is_live(self) -> bool:
        return self.state == LIVE and self.current_show is not None


@dataclass(frozen=True, slots=True)
class RadioState:
    station_identifier: str
    current_state: str
    current_occurrence_key: str | None
    current_identity: str | None
    current_title: str | None
    current_presenter: str | None
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    detected_live_at: datetime | None
    pending_occurrence_key: str | None
    pending_identity: str | None
    pending_count: int
    missing_count: int
    checked_at: datetime | None
    last_success_at: datetime | None
    error: str | None


@dataclass(frozen=True, slots=True)
class RadioTransition:
    kind: str
    occurrence_key: str
    changed_at: datetime


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _timestamp(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.isdigit():
            return _timestamp(int(cleaned))
        try:
            parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and value.is_integer():
        return max(0, int(value))
    return None


def _schedule_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("rows", "data", "schedule"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def parse_schedule(payload: Any, now: datetime) -> tuple[RadioShow, ...]:
    """Parse only scheduled streamer/DJ blocks, never AutoDJ playlists."""
    shows: list[RadioShow] = []
    for row in _schedule_rows(payload):
        source_type = (_text(row.get("type")) or "").casefold()
        if source_type not in {"streamer", "dj", "live"}:
            continue

        start = _timestamp(
            row.get("start_timestamp", row.get("start", row.get("start_time")))
        )
        end = _timestamp(
            row.get("end_timestamp", row.get("end", row.get("end_time")))
        )
        source_id_value = row.get("id", row.get("event_id"))
        source_id = str(source_id_value) if source_id_value is not None else None
        raw_name = _text(row.get("name"))
        explicit_title = _text(row.get("title"))
        description = _text(row.get("description"))
        presenter = _text(row.get("streamer_name") or row.get("presenter"))
        # Current AzuraCast schedule responses can expose the show title/name
        # separately while describing the linked streamer as ``Streamer: Name``.
        # Use that only as a conservative fallback; never infer a presenter from
        # an arbitrary description.
        if presenter is None and description:
            match = re.match(r"^streamer\s*:\s*(.+)$", description, re.IGNORECASE)
            if match:
                presenter = _text(match.group(1))
        if presenter is None and explicit_title and raw_name and raw_name != explicit_title:
            presenter = raw_name
        title = explicit_title or raw_name or "Scheduled live show"
        if start is not None:
            source_key = source_id or _normalize(title) or "streamer"
            occurrence_key = f"schedule:{source_key}:{int(start.timestamp())}"
        else:
            occurrence_key = None
        shows.append(
            RadioShow(
                occurrence_key=occurrence_key,
                source_event_id=source_id,
                source_type=source_type,
                title=title,
                presenter=presenter,
                scheduled_start=start,
                scheduled_end=end,
                description=description,
                artwork_url=_text(row.get("art") or row.get("artwork")),
            )
        )

    return tuple(
        sorted(
            shows,
            key=lambda show: show.scheduled_start
            or datetime.max.replace(tzinfo=timezone.utc),
        )
    )


def _current_scheduled_show(
    shows: tuple[RadioShow, ...], now: datetime, presenter: str | None
) -> RadioShow | None:
    current = [
        show
        for show in shows
        if show.scheduled_start is not None
        and show.scheduled_start <= now
        and (show.scheduled_end is None or now < show.scheduled_end)
    ]
    if not current:
        return None
    if presenter:
        needle = _normalize(presenter)
        for show in current:
            if needle and needle in _normalize(
                " ".join(filter(None, (show.presenter, show.title)))
            ):
                return show
    return current[0]


def parse_now_playing(
    payload: Any,
    *,
    station_identifier: str,
    public_url: str,
    checked_at: datetime | None = None,
    schedule_payload: Any = None,
    schedule_available: bool = False,
    schedule_error: str | None = None,
) -> RadioSnapshot:
    if not isinstance(payload, dict):
        raise ValueError("Now-playing API returned a non-object payload")
    if not all(key in payload for key in ("station", "now_playing", "live")):
        raise ValueError("Now-playing API did not return the AzuraCast shape")

    now = checked_at or utc_now()
    station = payload.get("station")
    station = station if isinstance(station, dict) else {}
    live = payload.get("live")
    live = live if isinstance(live, dict) else {}
    now_playing = payload.get("now_playing")
    now_playing = now_playing if isinstance(now_playing, dict) else {}
    song = now_playing.get("song")
    song = song if isinstance(song, dict) else {}
    listeners = payload.get("listeners")
    listeners = listeners if isinstance(listeners, dict) else {}

    station_name = _text(station.get("name")) or "Booty Bay Pirate Radio"
    station_timezone = _text(station.get("timezone"))
    schedule = (
        parse_schedule(schedule_payload, now) if schedule_available else tuple()
    )
    presenter = _text(live.get("streamer_name"))
    is_live = live.get("is_live") is True
    station_online = payload.get("is_online")

    current_track = _text(song.get("text"))
    if current_track is None:
        title = _text(song.get("title"))
        artist = _text(song.get("artist"))
        current_track = " - ".join(filter(None, (artist, title))) or None

    current_show: RadioShow | None = None
    if is_live:
        scheduled = _current_scheduled_show(schedule, now, presenter)
        if scheduled is not None:
            current_show = replace(
                scheduled,
                presenter=presenter or scheduled.presenter,
                artwork_url=_text(live.get("art")) or scheduled.artwork_url,
            )
        else:
            broadcast_start = _timestamp(live.get("broadcast_start"))
            presenter_key = _normalize(presenter or "live-dj") or "live-dj"
            occurrence_key = (
                f"live:{presenter_key}:{int(broadcast_start.timestamp())}"
                if broadcast_start is not None
                else None
            )
            current_show = RadioShow(
                occurrence_key=occurrence_key,
                source_event_id=None,
                source_type="streamer",
                title="Booty Bay Pirate Radio Live",
                presenter=presenter,
                scheduled_start=broadcast_start,
                scheduled_end=None,
                artwork_url=_text(live.get("art")),
            )
        state = LIVE
    elif station_online is True:
        state = AUTODJ
    elif station_online is False:
        state = OFFLINE
    else:
        state = UNKNOWN

    upcoming = tuple(
        show
        for show in schedule
        if show.scheduled_start is not None and show.scheduled_start > now
    )
    return RadioSnapshot(
        checked_at=now,
        source_ok=True,
        station_identifier=station_identifier,
        station_name=station_name,
        public_url=public_url,
        state=state,
        current_track=current_track,
        listeners=_integer(
            listeners.get("current")
            if listeners.get("current") is not None
            else listeners.get("total")
        ),
        current_show=current_show,
        upcoming=upcoming,
        schedule_available=schedule_available,
        schedule=schedule,
        station_timezone=station_timezone,
        platform_confirmed=True,
        schedule_error=schedule_error,
    )


def failed_snapshot(
    station_identifier: str,
    public_url: str,
    reason: str,
    *,
    checked_at: datetime | None = None,
) -> RadioSnapshot:
    return RadioSnapshot(
        checked_at=checked_at or utc_now(),
        source_ok=False,
        station_identifier=station_identifier,
        station_name="Booty Bay Pirate Radio",
        public_url=public_url,
        state=UNKNOWN,
        current_track=None,
        listeners=None,
        current_show=None,
        upcoming=(),
        schedule_available=False,
        station_timezone=None,
        platform_confirmed=False,
        error=reason,
    )


class RadioClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        station_shortcode: str,
        schedule_days: int = DEFAULT_SCHEDULE_DAYS,
    ):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.station_shortcode = station_shortcode
        self.schedule_days = max(1, schedule_days)
        encoded = quote(station_shortcode, safe="")
        self.now_playing_url = f"{self.base_url}/api/nowplaying/{encoded}"
        self._schedule_base_url = f"{self.base_url}/api/station/{encoded}/schedule"
        self.public_url = f"{self.base_url}/public/{encoded}"

    def schedule_url(self, now: datetime | None = None) -> str:
        """Schedule endpoint for a date window starting today, matching the public page."""
        current = (now or utc_now()).astimezone(timezone.utc)
        start = current.date()
        end = (current + timedelta(days=self.schedule_days)).date()
        return f"{self._schedule_base_url}?start={start.isoformat()}&end={end.isoformat()}"

    async def _json(self, url: str) -> Any:
        async with self.session.get(url) as response:
            response.raise_for_status()
            return await response.json(content_type=None)

    async def fetch(self) -> RadioSnapshot:
        checked_at = utc_now()
        try:
            now_playing = await self._json(self.now_playing_url)
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError) as exc:
            LOGGER.warning("Radio now-playing request failed: %s", type(exc).__name__)
            return failed_snapshot(
                self.station_shortcode,
                self.public_url,
                f"Now-playing request failed: {type(exc).__name__}",
                checked_at=checked_at,
            )

        schedule_payload: Any = None
        schedule_available = False
        schedule_error: str | None = None
        try:
            schedule_payload = await self._json(self.schedule_url(checked_at))
            if not isinstance(schedule_payload, (list, dict)):
                raise ValueError("Schedule API returned an unsupported payload")
            schedule_available = True
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError) as exc:
            schedule_error = f"Schedule unavailable: {type(exc).__name__}"
            LOGGER.info("Radio schedule is unavailable: %s", type(exc).__name__)

        try:
            return parse_now_playing(
                now_playing,
                station_identifier=self.station_shortcode,
                public_url=self.public_url,
                checked_at=checked_at,
                schedule_payload=schedule_payload,
                schedule_available=schedule_available,
                schedule_error=schedule_error,
            )
        except (ValueError, TypeError) as exc:
            LOGGER.warning("Radio now-playing payload was malformed: %s", exc)
            return failed_snapshot(
                self.station_shortcode,
                self.public_url,
                f"Malformed now-playing response: {type(exc).__name__}",
                checked_at=checked_at,
            )


# Known DJs and where they stream. Matched against the presenter name reported by
# the station schedule, so "DJ Whiski", "Whiski" and "whiski" all resolve.
DJ_TWITCH_STREAMS: dict[str, str] = {
    "whiski": "https://www.twitch.tv/djwhiski",
    "mossa": "https://www.twitch.tv/dj_mossa",
    "tekeela": "https://www.twitch.tv/tekeelatv",
    "sabellwind": "https://www.twitch.tv/sabellwind",
}


def parse_dj_streams(value: str | None) -> dict[str, str]:
    """Parse ``Name=https://...,Other=https://...`` into a normalized lookup table."""
    streams: dict[str, str] = {}
    for entry in (value or "").split(","):
        name, _, url = entry.partition("=")
        key = _normalize(name)
        url = url.strip()
        if key and url.startswith(("http://", "https://")):
            streams[key] = url
    return streams


def dj_stream_url(presenter: str | None, streams: dict[str, str] | None = None) -> str | None:
    """Return the Twitch URL for the DJ named in ``presenter``, or None if unknown."""
    if not presenter:
        return None
    table = DJ_TWITCH_STREAMS if streams is None else streams
    needle = _normalize(presenter)
    if not needle:
        return None
    if needle in table:
        return table[needle]
    # "dj-whiski" should still find "whiski"; longest key first avoids partial clashes.
    for key in sorted(table, key=len, reverse=True):
        if key and (key in needle or needle in key):
            return table[key]
    return None


def radio_identity(source_event_id: str | None, presenter: str | None, title: str | None) -> str:
    return "|".join(_normalize(part or "") for part in (source_event_id, presenter, title))


def same_broadcaster(
    previous_presenter: str | None,
    previous_identity: str | None,
    presenter: str | None,
    identity: str,
) -> bool:
    """True when two live detections are plausibly the same DJ's broadcast.

    A named presenter is compared by name; nameless streams fall back to the full
    identity so two different anonymous shows are not merged by accident.
    """
    if presenter and previous_presenter:
        return _normalize(presenter) == _normalize(previous_presenter)
    return bool(previous_identity) and previous_identity == identity


def fallback_occurrence_key(identity: str, detected_at: datetime) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"live:{digest}:{int(detected_at.timestamp())}"
