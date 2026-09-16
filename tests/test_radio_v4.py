from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from octotracker.config import Config
from octotracker.embeds import next_shows_embeds
from octotracker.database import Database
from octotracker.radio import (
    AUTODJ,
    LIVE,
    RadioShow,
    dj_stream_url,
    failed_snapshot,
    parse_dj_streams,
    parse_now_playing,
)
from octotracker.radio_monitor import RadioMonitor, radio_notification_message


class RadioParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)
        self.public_url = "https://radio.octowow.st/public/booty_bay_pirate_radio"

    def payload(self, *, live: bool) -> dict:
        return {
            "station": {"name": "Booty Bay Pirate Radio", "timezone": "UTC"},
            "is_online": True,
            "listeners": {"current": 23},
            "now_playing": {"song": {"text": "Artist - Track"}},
            "live": {
                "is_live": live,
                "streamer_name": "DJ Whiski" if live else None,
                "broadcast_start": int((self.now - timedelta(minutes=5)).timestamp()),
            },
        }

    def schedule(self, start: datetime | None = None) -> list[dict]:
        start = start or self.now - timedelta(minutes=10)
        return [
            {
                "id": 42,
                "type": "streamer",
                "title": "The Stranglethorn Hour",
                "streamer_name": "DJ Whiski",
                "start_timestamp": int(start.timestamp()),
                "end_timestamp": int((start + timedelta(hours=1)).timestamp()),
            },
            {
                "id": 99,
                "type": "playlist",
                "name": "Normal AutoDJ playlist",
                "start_timestamp": int(start.timestamp()),
                "end_timestamp": int((start + timedelta(hours=2)).timestamp()),
            },
        ]

    def test_autodj_is_not_live_show(self) -> None:
        snapshot = parse_now_playing(
            self.payload(live=False),
            station_identifier="booty_bay_pirate_radio",
            public_url=self.public_url,
            checked_at=self.now,
            schedule_payload=self.schedule(),
            schedule_available=True,
        )
        self.assertEqual(snapshot.state, AUTODJ)
        self.assertIsNone(snapshot.current_show)
        self.assertEqual(snapshot.current_track, "Artist - Track")
        self.assertEqual(snapshot.listeners, 23)

    def test_live_streamer_matches_scheduled_show_and_excludes_playlist(self) -> None:
        snapshot = parse_now_playing(
            self.payload(live=True),
            station_identifier="booty_bay_pirate_radio",
            public_url=self.public_url,
            checked_at=self.now,
            schedule_payload=self.schedule(),
            schedule_available=True,
        )
        self.assertEqual(snapshot.state, LIVE)
        self.assertIsNotNone(snapshot.current_show)
        assert snapshot.current_show is not None
        self.assertEqual(snapshot.current_show.title, "The Stranglethorn Hour")
        self.assertEqual(snapshot.current_show.presenter, "DJ Whiski")
        self.assertTrue(snapshot.current_show.occurrence_key.startswith("schedule:42:"))
        self.assertEqual(len(snapshot.upcoming), 0)

    def test_upcoming_schedule_orders_only_live_blocks(self) -> None:
        start = self.now + timedelta(hours=1)
        snapshot = parse_now_playing(
            self.payload(live=False),
            station_identifier="booty_bay_pirate_radio",
            public_url=self.public_url,
            checked_at=self.now,
            schedule_payload=self.schedule(start),
            schedule_available=True,
        )
        self.assertEqual(len(snapshot.upcoming), 1)
        self.assertEqual(snapshot.upcoming[0].title, "The Stranglethorn Hour")

    def test_missing_station_online_flag_is_unknown_not_autodj(self) -> None:
        payload = self.payload(live=False)
        payload.pop("is_online")
        snapshot = parse_now_playing(
            payload,
            station_identifier="booty_bay_pirate_radio",
            public_url=self.public_url,
            checked_at=self.now,
            schedule_available=False,
        )
        self.assertEqual(snapshot.state, "unknown")

    def test_schedule_can_extract_streamer_from_azuracast_description(self) -> None:
        start = self.now + timedelta(hours=1)
        snapshot = parse_now_playing(
            self.payload(live=False),
            station_identifier="booty_bay_pirate_radio",
            public_url=self.public_url,
            checked_at=self.now,
            schedule_payload=[
                {
                    "id": 123,
                    "type": "streamer",
                    "name": "Sunday Sessions",
                    "title": "Sunday Sessions",
                    "description": "Streamer: Ricky Prime",
                    "start_timestamp": int(start.timestamp()),
                    "end_timestamp": int((start + timedelta(hours=1)).timestamp()),
                }
            ],
            schedule_available=True,
        )
        self.assertEqual(len(snapshot.upcoming), 1)
        self.assertEqual(snapshot.upcoming[0].title, "Sunday Sessions")
        self.assertEqual(snapshot.upcoming[0].presenter, "Ricky Prime")

    def test_pirate_notification_fills_dj_and_role(self) -> None:
        message = radio_notification_message("DJ Whiski", 1234, self.public_url)
        self.assertIn("DJ Whiski", message)
        self.assertIn("<@&1234>", message)
        self.assertIn("GREETINGS, ALL YE", message)
        self.assertIn(self.public_url, message)
        self.assertIn(f"[Radio Website]({self.public_url})", message)
        self.assertNotIn("Twitch", message)

    def test_pirate_notification_lists_only_the_live_djs_twitch(self) -> None:
        message = radio_notification_message(
            "Mossa", 1234, self.public_url, "https://www.twitch.tv/dj_mossa"
        )
        self.assertIn("[Twitch Stream](https://www.twitch.tv/dj_mossa)!", message)
        self.assertNotIn("djwhiski", message)
        self.assertNotIn("tekeelatv", message)
        self.assertNotIn("sabellwind", message)

    def test_dj_stream_lookup_matches_schedule_names(self) -> None:
        self.assertEqual(dj_stream_url("Mossa"), "https://www.twitch.tv/dj_mossa")
        self.assertEqual(dj_stream_url("DJ Whiski"), "https://www.twitch.tv/djwhiski")
        self.assertEqual(dj_stream_url("tekeela"), "https://www.twitch.tv/tekeelatv")
        self.assertEqual(dj_stream_url("Sabellwind"), "https://www.twitch.tv/sabellwind")
        self.assertIsNone(dj_stream_url("Grog"))
        self.assertIsNone(dj_stream_url(None))
        custom = parse_dj_streams("Grog=https://www.twitch.tv/grog, bad=notaurl")
        self.assertEqual(custom, {"grog": "https://www.twitch.tv/grog"})
        self.assertEqual(dj_stream_url("DJ Grog", custom), "https://www.twitch.tv/grog")
        self.assertIsNone(dj_stream_url("Mossa", custom))


class NextShowsEmbedTests(unittest.TestCase):
    def test_lists_every_upcoming_show_with_absolute_and_relative_times(self) -> None:
        now = datetime(2026, 9, 16, 17, 0, tzinfo=timezone.utc)
        rows = []
        for index in range(30):
            start = now + timedelta(hours=2 + index * 24)
            rows.append({
                "id": 100 + index,
                "type": "streamer",
                "name": "Mossa" if index % 2 else "Whiski",
                "title": "Mossa" if index % 2 else "Whiski",
                "description": "Streamer: Mossa" if index % 2 else "Streamer: Whiski",
                "start_timestamp": int(start.timestamp()),
                "end_timestamp": int((start + timedelta(hours=2)).timestamp()),
            })
        snapshot = parse_now_playing(
            {
                "station": {"name": "Booty Bay Pirate Radio"},
                "is_online": True,
                "listeners": {"current": 1},
                "now_playing": {"song": {"text": "x"}},
                "live": {"is_live": False, "streamer_name": ""},
            },
            station_identifier="booty_bay_pirate_radio",
            public_url="https://radio.octowow.st/public/booty_bay_pirate_radio",
            checked_at=now,
            schedule_payload=rows,
            schedule_available=True,
        )
        embeds = next_shows_embeds(snapshot)
        self.assertEqual(len(embeds), 2)
        self.assertEqual(len(embeds[0].fields) + len(embeds[1].fields), 30)
        first = embeds[0].fields[0]
        first_ts = int((now + timedelta(hours=2)).timestamp())
        self.assertEqual(first.name, "1. Whiski — Whiski")
        self.assertIn(f"<t:{first_ts}:f> • <t:{first_ts}:R>", first.value)
        self.assertIn("[Twitch](https://www.twitch.tv/djwhiski)", first.value)
        self.assertIn("dj_mossa", embeds[0].fields[1].value)
        self.assertIn("30 upcoming shows", embeds[1].footer.text)

    def test_empty_schedule_says_so(self) -> None:
        snapshot = failed_snapshot("booty_bay_pirate_radio", "https://radio.example/x", "boom")
        self.assertIn("could not be read", next_shows_embeds(snapshot)[0].description)


class RadioMonitorConfigTests(unittest.TestCase):
    def test_disabled_or_unconfigured_radio_does_not_start_background_task(self) -> None:
        disabled = Config(
            token="test",
            guild_id=123,
            status_channel_id=None,
            alert_channel_id=None,
            announcement_channel_id=None,
            radio_enabled=False,
            radio_channel_id=1,
        )
        no_channel = Config(
            token="test",
            guild_id=123,
            status_channel_id=None,
            alert_channel_id=None,
            announcement_channel_id=None,
            radio_enabled=True,
            radio_channel_id=None,
        )
        placeholder = object()
        disabled_monitor = RadioMonitor(placeholder, disabled, placeholder, placeholder)
        no_channel_monitor = RadioMonitor(placeholder, no_channel, placeholder, placeholder)
        disabled_monitor.start()
        no_channel_monitor.start()
        self.assertIsNone(disabled_monitor._task)
        self.assertIsNone(no_channel_monitor._task)


class RadioDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "radio.db"
        self.db = Database(self.path)
        await self.db.connect()
        self.start = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp_dir.cleanup()

    def live_snapshot(
        self, at: datetime, *, event_id: str = "42", event_start: datetime | None = None
    ):
        start = (event_start or self.start).replace(second=0, microsecond=0)
        show = RadioShow(
            occurrence_key=f"schedule:{event_id}:{int(start.timestamp())}",
            source_event_id=event_id,
            source_type="streamer",
            title="Pirate Hour",
            presenter="DJ Whiski",
            scheduled_start=start,
            scheduled_end=start + timedelta(hours=1),
        )
        return parse_now_playing(
            {
                "station": {"name": "Booty Bay Pirate Radio"},
                "is_online": True,
                "listeners": {"current": 5},
                "now_playing": {"song": {"text": "Live audio"}},
                "live": {
                    "is_live": True,
                    "streamer_name": "DJ Whiski",
                    "broadcast_start": int(start.timestamp()),
                },
            },
            station_identifier="booty_bay_pirate_radio",
            public_url="https://radio.example/public/booty_bay_pirate_radio",
            checked_at=at,
            schedule_payload=[
                {
                    "id": event_id,
                    "type": "streamer",
                    "title": show.title,
                    "streamer_name": show.presenter,
                    "start_timestamp": int(start.timestamp()),
                    "end_timestamp": int((start + timedelta(hours=1)).timestamp()),
                }
            ],
            schedule_available=True,
        )

    def autodj_snapshot(self, at: datetime):
        return parse_now_playing(
            {
                "station": {"name": "Booty Bay Pirate Radio"},
                "is_online": True,
                "listeners": {"current": 4},
                "now_playing": {"song": {"text": "Artist - Song"}},
                "live": {"is_live": False, "streamer_name": None},
            },
            station_identifier="booty_bay_pirate_radio",
            public_url="https://radio.example/public/booty_bay_pirate_radio",
            checked_at=at,
            schedule_available=False,
        )

    async def test_two_confirmations_one_notification_and_restart_dedup(self) -> None:
        first = self.live_snapshot(self.start)
        state, transition = await self.db.apply_radio_snapshot(first, 2)
        self.assertNotEqual(state.current_state, LIVE)
        self.assertIsNone(transition)
        self.assertEqual(await self.db.pending_radio_notifications("booty_bay_pirate_radio"), [])

        second = self.live_snapshot(self.start + timedelta(seconds=30))
        state, transition = await self.db.apply_radio_snapshot(second, 2)
        self.assertEqual(state.current_state, LIVE)
        self.assertEqual(transition.kind, "go_live")
        pending = await self.db.pending_radio_notifications("booty_bay_pirate_radio")
        self.assertEqual(len(pending), 1)
        key = pending[0].occurrence_key
        await self.db.mark_radio_notification_sent(key, 777)

        third = self.live_snapshot(self.start + timedelta(seconds=60))
        _, transition = await self.db.apply_radio_snapshot(third, 2)
        self.assertIsNone(transition)
        self.assertEqual(await self.db.pending_radio_notifications("booty_bay_pirate_radio"), [])

        await self.db.close()
        self.db = Database(self.path)
        await self.db.connect()
        fourth = self.live_snapshot(self.start + timedelta(seconds=90))
        _, transition = await self.db.apply_radio_snapshot(fourth, 2)
        self.assertIsNone(transition)
        self.assertEqual(await self.db.pending_radio_notifications("booty_bay_pirate_radio"), [])

    async def test_api_failure_preserves_live_and_end_needs_confirmation(self) -> None:
        await self.db.apply_radio_snapshot(self.live_snapshot(self.start), 2)
        await self.db.apply_radio_snapshot(self.live_snapshot(self.start + timedelta(seconds=30)), 2)

        failure = failed_snapshot(
            "booty_bay_pirate_radio",
            "https://radio.example/public/booty_bay_pirate_radio",
            "timeout",
            checked_at=self.start + timedelta(seconds=60),
        )
        state, transition = await self.db.apply_radio_snapshot(failure, 2)
        self.assertEqual(state.current_state, LIVE)
        self.assertIsNone(transition)

        state, transition = await self.db.apply_radio_snapshot(
            self.autodj_snapshot(self.start + timedelta(seconds=90)), 2
        )
        self.assertEqual(state.current_state, LIVE)
        self.assertIsNone(transition)

        state, transition = await self.db.apply_radio_snapshot(
            self.autodj_snapshot(self.start + timedelta(seconds=120)), 2
        )
        self.assertEqual(state.current_state, AUTODJ)
        self.assertEqual(transition.kind, "ended")

    async def test_recurring_show_gets_distinct_later_occurrence(self) -> None:
        first_start = self.start
        await self.db.apply_radio_snapshot(self.live_snapshot(first_start), 2)
        await self.db.apply_radio_snapshot(self.live_snapshot(first_start + timedelta(seconds=30)), 2)
        first = (await self.db.pending_radio_notifications("booty_bay_pirate_radio"))[0]
        await self.db.mark_radio_notification_sent(first.occurrence_key, 1)

        await self.db.apply_radio_snapshot(self.autodj_snapshot(first_start + timedelta(hours=1, seconds=1)), 2)
        await self.db.apply_radio_snapshot(self.autodj_snapshot(first_start + timedelta(hours=1, seconds=31)), 2)

        later = self.start + timedelta(days=7)
        await self.db.apply_radio_snapshot(self.live_snapshot(later, event_start=later), 2)
        await self.db.apply_radio_snapshot(self.live_snapshot(later + timedelta(seconds=30), event_start=later), 2)
        pending = await self.db.pending_radio_notifications("booty_bay_pirate_radio")
        self.assertEqual(len(pending), 1)
        self.assertNotEqual(pending[0].occurrence_key, first.occurrence_key)

    def live_snapshot_with_broadcast(
        self, at: datetime, *, broadcast_start: datetime, presenter: str = "DJ Whiski",
        schedule: bool = True,
    ):
        """Realistic AzuraCast payload: the DJ's own broadcast_start, plus the slot schedule."""
        slot = self.start
        return parse_now_playing(
            {
                "station": {"name": "Booty Bay Pirate Radio"},
                "is_online": True,
                "listeners": {"current": 5},
                "now_playing": {"song": {"text": "Live audio"}},
                "live": {
                    "is_live": True,
                    "streamer_name": presenter,
                    "broadcast_start": int(broadcast_start.timestamp()),
                },
            },
            station_identifier="booty_bay_pirate_radio",
            public_url="https://radio.example/public/booty_bay_pirate_radio",
            checked_at=at,
            schedule_payload=[
                {
                    "id": 10,
                    "type": "streamer",
                    "name": "DJ Whiski",
                    "title": "DJ Whiski",
                    "description": "Streamer: DJ Whiski",
                    "start_timestamp": int(slot.timestamp()),
                    "end_timestamp": int((slot + timedelta(hours=1)).timestamp()),
                }
            ] if schedule else [],
            schedule_available=True,
        )

    async def _drain(self) -> list[str]:
        sent = []
        for occurrence in await self.db.pending_radio_notifications("booty_bay_pirate_radio"):
            await self.db.mark_radio_notification_sent(occurrence.occurrence_key, 1)
            sent.append(occurrence.occurrence_key)
        return sent

    async def test_early_start_and_overrun_is_one_occurrence(self) -> None:
        # The DJ connects 5 minutes before the slot and stays 10 minutes past its end.
        broadcast_start = self.start - timedelta(minutes=5)
        pinged: list[str] = []
        at = broadcast_start
        while at <= self.start + timedelta(hours=1, minutes=10):
            state, _ = await self.db.apply_radio_snapshot(
                self.live_snapshot_with_broadcast(at, broadcast_start=broadcast_start), 2
            )
            if state.current_state == LIVE:
                pinged.extend(await self._drain())
            at += timedelta(seconds=10)

        self.assertEqual(len(pinged), 1)
        self.assertTrue(pinged[0].startswith("live:dj-whiski:"))
        state = await self.db.get_radio_state("booty_bay_pirate_radio")
        self.assertEqual(state.current_occurrence_key, pinged[0])
        # The history row picked up the schedule metadata once the slot started.
        rows = await self.db.recent_radio_occurrences("booty_bay_pirate_radio", 5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source_event_id, "10")
        self.assertIsNotNone(rows[0].scheduled_end)

    async def test_reconnect_within_grace_does_not_ping_again(self) -> None:
        # Unscheduled DJ drops for 25 seconds; AzuraCast reports a new broadcast_start.
        first_start = self.start + timedelta(hours=3)
        second_start = first_start + timedelta(minutes=10, seconds=25)
        pinged: list[str] = []

        async def feed(at, snapshot):
            state, _ = await self.db.apply_radio_snapshot(snapshot, 2)
            if snapshot.is_live and state.current_state == LIVE:
                pinged.extend(await self._drain())

        at = first_start
        while at <= first_start + timedelta(minutes=10):
            await feed(at, self.live_snapshot_with_broadcast(
                at, broadcast_start=first_start, presenter="Grog", schedule=False))
            at += timedelta(seconds=10)
        await feed(at, self.autodj_snapshot(at))
        await feed(at + timedelta(seconds=10), self.autodj_snapshot(at + timedelta(seconds=10)))
        at = second_start
        while at <= second_start + timedelta(minutes=5):
            await feed(at, self.live_snapshot_with_broadcast(
                at, broadcast_start=second_start, presenter="Grog", schedule=False))
            at += timedelta(seconds=10)

        self.assertEqual(len(pinged), 1)
        rows = await self.db.recent_radio_occurrences("booty_bay_pirate_radio", 5)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].detected_end_at)

    async def test_different_dj_after_dropout_is_a_new_show(self) -> None:
        first_start = self.start + timedelta(hours=3)
        for offset in (0, 10):
            await self.db.apply_radio_snapshot(self.live_snapshot_with_broadcast(
                first_start + timedelta(seconds=offset), broadcast_start=first_start,
                presenter="Grog", schedule=False), 2)
        first = await self._drain()
        for offset in (20, 30):
            await self.db.apply_radio_snapshot(self.autodj_snapshot(first_start + timedelta(seconds=offset)), 2)
        second_start = first_start + timedelta(seconds=40)
        for offset in (40, 50):
            await self.db.apply_radio_snapshot(self.live_snapshot_with_broadcast(
                first_start + timedelta(seconds=offset), broadcast_start=second_start,
                presenter="Mossa", schedule=False), 2)
        second = await self._drain()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0], second[0])


class _FakeMessage:
    def __init__(self, message_id: int):
        self.id = message_id


class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return _FakeMessage(len(self.sent))


class _FakeBot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel if channel_id == 1 else None

    async def fetch_channel(self, channel_id):
        return self.channel


class _SequenceRadioClient:
    public_url = "https://radio.example/public/booty_bay_pirate_radio"

    def __init__(self, snapshots):
        self.snapshots = list(snapshots)

    async def fetch(self):
        return self.snapshots.pop(0)


class RadioMonitorDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "radio-monitor.db"
        self.db = Database(self.path)
        await self.db.connect()
        self.start = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)
        self.config = self.config_with()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp_dir.cleanup()

    def snapshot(self, at, live):
        payload = {
            "station": {"name": "Booty Bay Pirate Radio"},
            "is_online": True,
            "listeners": {"current": 5},
            "now_playing": {"song": {"text": "Audio"}},
            "live": {
                "is_live": live,
                "streamer_name": "DJ Whiski" if live else None,
                "broadcast_start": int(self.start.timestamp()),
            },
        }
        schedule = [
            {
                "id": 42,
                "type": "streamer",
                "title": "Pirate Hour",
                "streamer_name": "DJ Whiski",
                "start_timestamp": int(self.start.timestamp()),
                "end_timestamp": int((self.start + timedelta(hours=1)).timestamp()),
            }
        ]
        return parse_now_playing(
            payload,
            station_identifier="booty_bay_pirate_radio",
            public_url=_SequenceRadioClient.public_url,
            checked_at=at,
            schedule_payload=schedule,
            schedule_available=True,
        )

    def config_with(self, **overrides) -> Config:
        return Config(
            token="test",
            guild_id=123,
            status_channel_id=None,
            alert_channel_id=None,
            announcement_channel_id=None,
            radio_channel_id=1,
            radio_ping_role_id=1547108101747118100,
            radio_go_live_confirmations=2,
            **overrides,
        )

    async def test_scheduled_show_is_announced_at_start_once(self) -> None:
        # The DJ never connects: the ping still goes out at the scheduled start.
        channel = _FakeChannel()
        client = _SequenceRadioClient([
            self.snapshot(self.start - timedelta(seconds=10), False),
            self.snapshot(self.start, False),
            self.snapshot(self.start + timedelta(seconds=10), False),
            self.snapshot(self.start + timedelta(minutes=30), True),
            self.snapshot(self.start + timedelta(minutes=30, seconds=10), True),
        ])
        monitor = RadioMonitor(_FakeBot(channel), self.config, self.db, client)
        await monitor.check()
        self.assertEqual(len(channel.sent), 0)
        await monitor.check()
        self.assertEqual(len(channel.sent), 1)
        self.assertIn("<@&1547108101747118100>", channel.sent[0]["content"])
        self.assertIn("DJ Whiski", channel.sent[0]["content"])
        self.assertIn("[Twitch Stream](https://www.twitch.tv/djwhiski)", channel.sent[0]["content"])
        self.assertNotIn("dj_mossa", channel.sent[0]["content"])
        self.assertEqual(channel.sent[0]["allowed_mentions"].roles[0].id, 1547108101747118100)
        for _ in range(3):
            await monitor.check()
        self.assertEqual(len(channel.sent), 1)

    async def test_missed_start_is_announced_within_grace_only(self) -> None:
        channel = _FakeChannel()
        client = _SequenceRadioClient([self.snapshot(self.start + timedelta(minutes=5), False)])
        monitor = RadioMonitor(_FakeBot(channel), self.config, self.db, client)
        await monitor.check()
        self.assertEqual(len(channel.sent), 1)

        late_channel = _FakeChannel()
        late_db = Database(Path(self.temp_dir.name) / "late.db")
        await late_db.connect()
        try:
            client = _SequenceRadioClient([self.snapshot(self.start + timedelta(minutes=15), False)])
            monitor = RadioMonitor(_FakeBot(late_channel), self.config, late_db, client)
            await monitor.check()
            self.assertEqual(late_channel.sent, [])
        finally:
            await late_db.close()

    async def test_channel_failure_retries_next_poll(self) -> None:
        class _BrokenChannel(_FakeChannel):
            def __init__(self):
                super().__init__()
                self.fail = True

            async def send(self, **kwargs):
                if self.fail:
                    import discord
                    raise discord.HTTPException(type("R", (), {"status": 500, "reason": "x"})(), "boom")
                return await super().send(**kwargs)

        channel = _BrokenChannel()
        client = _SequenceRadioClient([
            self.snapshot(self.start, False),
            self.snapshot(self.start + timedelta(seconds=10), False),
        ])
        monitor = RadioMonitor(_FakeBot(channel), self.config, self.db, client)
        await monitor.check()
        self.assertEqual(channel.sent, [])
        channel.fail = False
        await monitor.check()
        self.assertEqual(len(channel.sent), 1)

    async def test_live_alerts_are_off_by_default(self) -> None:
        # Unscheduled DJ at 23:00: no schedule entry, so nothing is announced.
        channel = _FakeChannel()
        late = self.start + timedelta(hours=3)
        client = _SequenceRadioClient([
            self.snapshot(late, True),
            self.snapshot(late + timedelta(seconds=30), True),
            self.snapshot(late + timedelta(seconds=60), True),
        ])
        monitor = RadioMonitor(_FakeBot(channel), self.config, self.db, client)
        for _ in range(3):
            await monitor.check()
        self.assertEqual(channel.sent, [])

    async def test_live_alerts_opt_in_sends_once_and_not_for_announced_slot(self) -> None:
        config = self.config_with(radio_live_alerts=True)
        # Unscheduled: live detection still needs two confirmations, then sends once.
        channel = _FakeChannel()
        late = self.start + timedelta(hours=3)
        client = _SequenceRadioClient([
            self.snapshot(late, True),
            self.snapshot(late + timedelta(seconds=30), True),
            self.snapshot(late + timedelta(seconds=60), True),
        ])
        monitor = RadioMonitor(_FakeBot(channel), config, self.db, client)
        await monitor.check()
        self.assertEqual(len(channel.sent), 0)
        await monitor.check()
        self.assertEqual(len(channel.sent), 1)
        await monitor.check()
        self.assertEqual(len(channel.sent), 1)

        # Scheduled: announced from the schedule at 20:00, and the live tracker
        # confirming the same slot afterwards must not ping a second time.
        channel = _FakeChannel()
        other_db = Database(Path(self.temp_dir.name) / "sched.db")
        await other_db.connect()
        try:
            client = _SequenceRadioClient([
                self.snapshot(self.start, True),
                self.snapshot(self.start + timedelta(seconds=30), True),
                self.snapshot(self.start + timedelta(seconds=60), True),
            ])
            monitor = RadioMonitor(_FakeBot(channel), config, other_db, client)
            for _ in range(3):
                await monitor.check()
            self.assertEqual(len(channel.sent), 1)
            self.assertEqual(
                await other_db.pending_radio_notifications("booty_bay_pirate_radio"), []
            )
        finally:
            await other_db.close()

    async def test_non_live_poll_never_sends_stale_pending_notification(self) -> None:
        # Confirm a live occurrence directly in the database but leave its
        # notification unsent, simulating a crash/channel failure.
        config = self.config_with(radio_live_alerts=True)
        await self.db.apply_radio_snapshot(self.snapshot(self.start, True), 2)
        await self.db.apply_radio_snapshot(
            self.snapshot(self.start + timedelta(seconds=30), True), 2
        )
        self.assertEqual(
            len(await self.db.pending_radio_notifications("booty_bay_pirate_radio")),
            1,
        )

        channel = _FakeChannel()
        # Outside the schedule grace window so only the live path could send.
        client = _SequenceRadioClient([
            self.snapshot(self.start + timedelta(minutes=20), False)
        ])
        monitor = RadioMonitor(_FakeBot(channel), config, self.db, client)
        await monitor.check()
        self.assertEqual(channel.sent, [])


if __name__ == "__main__":
    unittest.main()
