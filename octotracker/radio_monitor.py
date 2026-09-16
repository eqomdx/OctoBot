from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta

import discord

from .config import Config
from .database import Database, RadioOccurrence
from .embeds import radio_live_embed
from .radio import (
    DJ_TWITCH_STREAMS,
    LIVE,
    RadioClient,
    RadioShow,
    RadioSnapshot,
    dj_stream_url,
    parse_dj_streams,
)


LOGGER = logging.getLogger(__name__)
RADIO_LISTEN_URL = (
    "https://radio.octowow.st/public/booty_bay_pirate_radio"
)
# A scheduled show is announced once its start time has passed. If the bot was down
# at that moment it still announces on the next poll, but not once the show is this
# far in: a late ping is worse than none.
SCHEDULE_ANNOUNCE_GRACE = timedelta(minutes=10)


def radio_notification_message(
    dj_name: str,
    role_id: int | None,
    listen_url: str = RADIO_LISTEN_URL,
    twitch_url: str | None = None,
) -> str:
    audience = f"<@&{role_id}> " if role_id is not None else ""
    website = f"[Radio Website]({listen_url})"
    ways = (
        f"Tune in through the in-game radio, the {website}, "
        f"or directly on their [Twitch Stream]({twitch_url})!"
        if twitch_url
        else f"Tune in through the in-game radio or the {website}!"
    )
    return (
        f"GREETINGS, ALL YE {audience}LISTENERS!\n"
        "YARRR! This be the Booty Bay Pirate Radio Announcement Service, "
        f"lettin’ all ye landlubbers know that {dj_name} has just gone LIVE "
        "on the airwaves!\n"
        f"{ways}\n"
        "Raise the sails, turn it up, and enjoy the show! ☠️📻"
    )


class RadioMonitor:
    def __init__(
        self,
        bot: discord.Client,
        config: Config,
        database: Database,
        client: RadioClient,
    ):
        self.bot = bot
        self.config = config
        self.database = database
        self.client = client
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.dj_streams = (
            parse_dj_streams(config.radio_dj_streams)
            if config.radio_dj_streams
            else dict(DJ_TWITCH_STREAMS)
        )

    def start(self) -> None:
        if self._task is not None:
            return
        if not self.config.radio_enabled:
            LOGGER.info("Radio monitoring is disabled by configuration")
            return
        if self.config.radio_channel_id is None:
            LOGGER.info("Radio monitoring is disabled because no channel is configured")
            return
        self._task = asyncio.create_task(self._loop(), name="radio-monitor")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Unexpected error in radio monitor")
            await asyncio.sleep(self.config.radio_poll_seconds)

    async def check(self) -> RadioSnapshot:
        async with self._lock:
            snapshot = await self.client.fetch()
            state, _ = await self.database.apply_radio_snapshot(
                snapshot, self.config.radio_go_live_confirmations
            )
            if snapshot.source_ok and snapshot.schedule_available:
                await self._announce_scheduled_shows(snapshot)
            # Never deliver a stale "now live" alert after the source has already
            # moved back to AutoDJ/offline. Only the currently confirmed live
            # occurrence is eligible for delivery.
            if (
                self.config.radio_live_alerts
                and snapshot.source_ok
                and snapshot.is_live
                and state.current_state == LIVE
                and state.current_occurrence_key is not None
            ):
                await self._send_pending_notifications(
                    state.current_occurrence_key
                )
            return snapshot

    def _due_scheduled_shows(self, snapshot: RadioSnapshot) -> list[RadioShow]:
        """Scheduled shows whose start time has just passed and are not too far in."""
        now = snapshot.checked_at
        due = []
        for show in snapshot.schedule:
            if show.occurrence_key is None or show.scheduled_start is None:
                continue
            if show.scheduled_start > now:
                continue
            if now - show.scheduled_start > SCHEDULE_ANNOUNCE_GRACE:
                continue
            if show.scheduled_end is not None and now >= show.scheduled_end:
                continue
            due.append(show)
        return due

    async def _announce_scheduled_shows(self, snapshot: RadioSnapshot) -> None:
        channel_id = self.config.radio_channel_id
        if channel_id is None:
            return
        due = self._due_scheduled_shows(snapshot)
        if not due:
            return
        station = self.config.radio_station_shortcode
        already = await self.database.announced_radio_schedule_keys(
            station, (show.occurrence_key for show in due)
        )
        for show in due:
            if show.occurrence_key in already:
                continue
            occurrence = RadioOccurrence(
                occurrence_key=show.occurrence_key,
                station_identifier=station,
                source_event_id=show.source_event_id,
                title=show.title,
                presenter=show.presenter,
                scheduled_start=show.scheduled_start,
                scheduled_end=show.scheduled_end,
                detected_live_at=show.scheduled_start,
                detected_end_at=None,
                description=show.description,
                artwork_url=show.artwork_url,
                notification_sent=False,
                notification_message_id=None,
            )
            message_id = await self._send_occurrence(channel_id, occurrence)
            if message_id is None:
                # Channel trouble: retry on the next poll while still inside the grace window.
                break
            await self.database.mark_radio_schedule_announced(
                station, show, snapshot.checked_at, message_id
            )
            LOGGER.info(
                "Announced scheduled radio show %s (%s)", show.title, show.occurrence_key
            )

    async def fetch_current(self) -> RadioSnapshot:
        """Fetch display data without changing notification/deduplication state."""
        return await self.client.fetch()

    async def _send_pending_notifications(self, occurrence_key: str) -> None:
        channel_id = self.config.radio_channel_id
        if channel_id is None:
            return
        station = self.config.radio_station_shortcode
        # A slot already announced from the schedule is never announced again here.
        already = await self.database.announced_radio_schedule_keys(station, (occurrence_key,))
        for occurrence in await self.database.pending_radio_notifications(station):
            if occurrence.occurrence_key != occurrence_key:
                continue
            if occurrence.occurrence_key in already:
                await self.database.mark_radio_notification_sent(occurrence.occurrence_key, None)
                continue
            message_id = await self._send_occurrence(channel_id, occurrence)
            if message_id is None:
                break
            await self.database.mark_radio_notification_sent(
                occurrence.occurrence_key, message_id
            )

    async def _send_occurrence(
        self, channel_id: int, occurrence: RadioOccurrence
    ) -> int | None:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                LOGGER.exception("Could not access radio channel %s", channel_id)
                return None
        if not hasattr(channel, "send"):
            LOGGER.error("Radio channel %s cannot receive messages", channel_id)
            return None

        role_id = self.config.radio_ping_role_id
        allowed_mentions = (
            discord.AllowedMentions(
                everyone=False,
                users=False,
                roles=[discord.Object(id=role_id)],
                replied_user=False,
            )
            if role_id is not None
            else discord.AllowedMentions.none()
        )
        dj_name = occurrence.presenter or occurrence.title
        twitch_url = dj_stream_url(dj_name, self.dj_streams)
        try:
            message = await channel.send(
                content=radio_notification_message(
                    dj_name, role_id, self.client.public_url, twitch_url
                ),
                embed=radio_live_embed(occurrence, self.client.public_url, twitch_url),
                allowed_mentions=allowed_mentions,
            )
            return message.id
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Could not send radio notification to %s", channel_id)
            return None
