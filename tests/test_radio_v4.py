from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from octotracker.config import Config
from octotracker.database import Database
from octotracker.radio import (
    AUTODJ,
    LIVE,
    RadioShow,
    failed_snapshot,
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
        self.config = Config(
            token="test",
            guild_id=123,
            status_channel_id=None,
            alert_channel_id=None,
            announcement_channel_id=None,
            radio_channel_id=1,
            radio_ping_role_id=None,
            radio_go_live_confirmations=2,
        )

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

    async def test_live_confirmation_sends_once(self) -> None:
        channel = _FakeChannel()
        client = _SequenceRadioClient([
            self.snapshot(self.start, True),
            self.snapshot(self.start + timedelta(seconds=30), True),
            self.snapshot(self.start + timedelta(seconds=60), True),
        ])
        monitor = RadioMonitor(_FakeBot(channel), self.config, self.db, client)
        await monitor.check()
        self.assertEqual(len(channel.sent), 0)
        await monitor.check()
        self.assertEqual(len(channel.sent), 1)
        await monitor.check()
        self.assertEqual(len(channel.sent), 1)

    async def test_non_live_poll_never_sends_stale_pending_notification(self) -> None:
        # Confirm a live occurrence directly in the database but leave its
        # notification unsent, simulating a crash/channel failure.
        await self.db.apply_radio_snapshot(self.snapshot(self.start, True), 2)
        await self.db.apply_radio_snapshot(
            self.snapshot(self.start + timedelta(seconds=30), True), 2
        )
        self.assertEqual(
            len(await self.db.pending_radio_notifications("booty_bay_pirate_radio")),
            1,
        )

        channel = _FakeChannel()
        client = _SequenceRadioClient([
            self.snapshot(self.start + timedelta(minutes=2), False)
        ])
        monitor = RadioMonitor(_FakeBot(channel), self.config, self.db, client)
        await monitor.check()
        self.assertEqual(channel.sent, [])



if __name__ == "__main__":
    unittest.main()
