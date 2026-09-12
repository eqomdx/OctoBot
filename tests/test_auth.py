from __future__ import annotations

import asyncio
import os
import struct
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from octotracker.auth import (
    MALFORMED_RESPONSE,
    RESPONSIVE,
    UNKNOWN,
    UNRESPONSIVE,
    AuthProbe,
    AuthProbeResult,
    build_logon_challenge,
)
from octotracker.config import _auth_port
from octotracker.connectivity import REACHABLE, UNREACHABLE
from octotracker.database import AuthIncident, Database, Incident
from octotracker.embeds import (
    auth_degraded_embed,
    auth_recovery_embed,
    authcheck_embed,
    incidents_embed,
)


VALID_FAILURE_RESPONSE = b"\x00\x00\x04"  # Defined UNKNOWN_ACCOUNT result.
VALID_SUCCESS_RESPONSE = b"".join(
    (
        b"\x00\x00\x00",
        b"B" * 32,
        b"\x01\x07",
        b"\x20",
        b"P" * 32,
        b"S" * 32,
        b"C" * 16,
        b"\x00",
    )
)


class AuthChallengeTests(unittest.TestCase):
    def test_protocol_three_vanilla_challenge_matches_reference_vector(self) -> None:
        expected = bytes(
            (
                0,
                3,
                31,
                0,
                87,
                111,
                87,
                0,
                1,
                12,
                1,
                243,
                22,
                54,
                56,
                120,
                0,
                110,
                105,
                87,
                0,
                66,
                71,
                110,
                101,
                60,
                0,
                0,
                0,
                127,
                0,
                0,
                1,
                1,
                65,
            )
        )
        self.assertEqual(
            build_logon_challenge("A", timezone_offset_minutes=60), expected
        )

    def test_synthetic_identity_is_bounded_and_contains_no_credentials(self) -> None:
        packet = build_logon_challenge()
        body_size = struct.unpack("<H", packet[2:4])[0]
        self.assertEqual(body_size, len(packet) - 4)
        account_length = packet[33]
        self.assertLessEqual(account_length, 16)
        self.assertEqual(account_length, len(packet[34:]))
        self.assertNotIn(b"password", packet.lower())
        with self.assertRaises(ValueError):
            build_logon_challenge("X" * 17)


class AuthProbeTests(unittest.IsolatedAsyncioTestCase):
    async def _serve(self, response: bytes | None, *, delay: float = 0):
        packets: list[bytes] = []
        closed = asyncio.Event()

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                header = await reader.readexactly(4)
                size = struct.unpack("<H", header[2:4])[0]
                packets.append(header + await reader.readexactly(size))
                if delay:
                    await asyncio.sleep(delay)
                if response is not None:
                    writer.write(response)
                    await writer.drain()
                    if len(response) < 3:
                        return
                await reader.read()
            finally:
                closed.set()
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        return server, port, packets, closed

    async def test_defined_failure_is_responsive_and_connection_is_closed(self) -> None:
        server, port, packets, closed = await self._serve(VALID_FAILURE_RESPONSE)
        try:
            result = await AuthProbe("127.0.0.1", port, 1).probe()
            await asyncio.wait_for(closed.wait(), 1)
        finally:
            server.close()
            await server.wait_closed()

        self.assertEqual(result.tcp_status, REACHABLE)
        self.assertEqual(result.auth_status, RESPONSIVE)
        self.assertEqual(result.result_code, 4)
        self.assertIsNotNone(result.latency_ms)
        self.assertEqual(len(packets), 1)

    async def test_fully_framed_success_challenge_is_responsive(self) -> None:
        server, port, _, closed = await self._serve(VALID_SUCCESS_RESPONSE)
        try:
            result = await AuthProbe("127.0.0.1", port, 1).probe()
            await asyncio.wait_for(closed.wait(), 1)
        finally:
            server.close()
            await server.wait_closed()
        self.assertEqual(result.auth_status, RESPONSIVE)
        self.assertEqual(result.result_code, 0)

    async def test_delayed_valid_response_records_latency(self) -> None:
        server, port, _, closed = await self._serve(
            VALID_FAILURE_RESPONSE, delay=0.04
        )
        try:
            result = await AuthProbe("127.0.0.1", port, 1).probe()
            await asyncio.wait_for(closed.wait(), 1)
        finally:
            server.close()
            await server.wait_closed()
        self.assertEqual(result.auth_status, RESPONSIVE)
        self.assertGreaterEqual(result.latency_ms or 0, 25)

    async def test_silent_server_is_unresponsive_but_tcp_reachable(self) -> None:
        server, port, _, closed = await self._serve(None)
        try:
            result = await AuthProbe("127.0.0.1", port, 0.04).probe()
            await asyncio.wait_for(closed.wait(), 1)
        finally:
            server.close()
            await server.wait_closed()
        self.assertEqual(result.tcp_status, REACHABLE)
        self.assertEqual(result.auth_status, UNRESPONSIVE)

    async def test_bad_and_partial_responses_are_malformed(self) -> None:
        for response in (b"\x01\x00\x04", b"\x00\x00"):
            with self.subTest(response=response):
                server, port, _, closed = await self._serve(response)
                try:
                    result = await AuthProbe("127.0.0.1", port, 1).probe()
                    await asyncio.wait_for(closed.wait(), 1)
                finally:
                    server.close()
                    await server.wait_closed()
                self.assertEqual(result.tcp_status, REACHABLE)
                self.assertEqual(result.auth_status, MALFORMED_RESPONSE)

    async def test_refused_and_connect_timeout_keep_auth_unknown(self) -> None:
        server = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        refused = await AuthProbe("127.0.0.1", port, 1).probe()
        self.assertEqual(refused.tcp_status, UNREACHABLE)
        self.assertEqual(refused.auth_status, UNKNOWN)

        async def never_connect(*_args, **_kwargs):
            await asyncio.sleep(1)

        with mock.patch(
            "octotracker.auth.asyncio.open_connection", side_effect=never_connect
        ):
            timed_out = await AuthProbe("example.invalid", 3724, 0.02).probe()
        self.assertEqual(timed_out.tcp_status, UNREACHABLE)
        self.assertEqual(timed_out.auth_status, UNKNOWN)

        disabled = await AuthProbe("example.invalid", None, 1).probe()
        self.assertEqual(disabled.tcp_status, UNKNOWN)
        self.assertEqual(disabled.auth_status, UNKNOWN)

    async def test_probe_serializes_overlapping_calls(self) -> None:
        active = 0
        maximum_active = 0
        connections = 0

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            nonlocal active, maximum_active, connections
            header = await reader.readexactly(4)
            size = struct.unpack("<H", header[2:4])[0]
            await reader.readexactly(size)
            connections += 1
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.04)
            writer.write(VALID_FAILURE_RESPONSE)
            await writer.drain()
            active -= 1
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        probe = AuthProbe("127.0.0.1", port, 1)
        try:
            first, second = await asyncio.gather(probe.probe(), probe.probe())
        finally:
            server.close()
            await server.wait_closed()
        self.assertEqual(first.auth_status, RESPONSIVE)
        self.assertEqual(second.auth_status, RESPONSIVE)
        self.assertEqual(connections, 2)
        self.assertEqual(maximum_active, 1)


class AuthEmbedTests(unittest.TestCase):
    def test_diagnostic_alert_and_combined_incident_embeds(self) -> None:
        started = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        ended = started + timedelta(minutes=3)
        auth_incident = AuthIncident(
            incident_id=2,
            started_at=started,
            ended_at=ended,
            initial_status=UNRESPONSIVE,
            latest_status=UNRESPONSIVE,
            tcp_status=REACHABLE,
            reason="Authentication response timed out",
            latency_ms=12,
        )
        official_incident = Incident(1, started, ended, "partial", "offline")
        history = incidents_embed([official_incident], [auth_incident])
        self.assertEqual(len(history.fields), 2)
        self.assertTrue(any("Authentication" in field.name for field in history.fields))

        degraded = auth_degraded_embed(auth_incident)
        recovered = auth_recovery_embed(auth_incident)
        self.assertIn("authentication degraded", degraded.title.lower())
        self.assertIn("authentication recovered", recovered.title.lower())

        diagnostic = authcheck_embed(
            AuthProbeResult(
                checked_at=ended,
                tcp_status=REACHABLE,
                auth_status=RESPONSIVE,
                host="127.0.0.1",
                port=3724,
                latency_ms=12,
                reason="Valid logon challenge response (result 4)",
                result_code=4,
            )
        )
        self.assertIn("does not alter", diagnostic.description)
        self.assertLessEqual(len(diagnostic), 6000)


class AuthStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "auth.db"
        self.database = Database(self.path)
        await self.database.connect()
        self.start = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temp_dir.cleanup()

    def result(self, offset: int, status: str) -> AuthProbeResult:
        tcp = UNREACHABLE if status == UNKNOWN else REACHABLE
        return AuthProbeResult(
            checked_at=self.start + timedelta(seconds=offset),
            tcp_status=tcp,
            auth_status=status,
            host="127.0.0.1",
            port=3724,
            latency_ms=None if status == UNKNOWN else 10,
            reason=status,
            result_code=4 if status == RESPONSIVE else None,
        )

    async def apply(self, offset: int, status: str):
        return await self.database.apply_auth_result(self.result(offset, status), 3, 2)

    async def test_confirmation_incident_alert_dedup_recovery_and_restart(self) -> None:
        state, transition = await self.apply(0, RESPONSIVE)
        self.assertEqual(state.status, UNKNOWN)
        self.assertIsNone(transition)
        state, transition = await self.apply(1, RESPONSIVE)
        self.assertEqual(state.status, RESPONSIVE)
        self.assertIsNotNone(transition)

        for offset in (2, 3):
            state, transition = await self.apply(offset, UNRESPONSIVE)
            self.assertEqual(state.status, RESPONSIVE)
            self.assertIsNone(transition)
        state, transition = await self.apply(4, UNRESPONSIVE)
        self.assertEqual(state.status, UNRESPONSIVE)
        self.assertEqual(transition.kind, "degraded")

        incidents = await self.database.recent_auth_incidents()
        self.assertEqual(len(incidents), 1)
        pending = await self.database.pending_auth_incident_alerts()
        self.assertEqual([item.kind for item in pending], ["degraded"])
        await self.database.mark_auth_incident_alert_sent(
            incidents[0].incident_id, "degraded"
        )
        self.assertEqual(await self.database.pending_auth_incident_alerts(), [])

        state, transition = await self.apply(5, MALFORMED_RESPONSE)
        self.assertEqual(state.status, MALFORMED_RESPONSE)
        self.assertIsNotNone(transition)
        self.assertIsNone(transition.kind)
        self.assertEqual(len(await self.database.recent_auth_incidents()), 1)
        self.assertEqual(await self.database.pending_auth_incident_alerts(), [])

        state, transition = await self.apply(6, RESPONSIVE)
        self.assertEqual(state.status, MALFORMED_RESPONSE)
        self.assertIsNone(transition)
        state, transition = await self.apply(7, RESPONSIVE)
        self.assertEqual(state.status, RESPONSIVE)
        self.assertEqual(transition.kind, "recovery")
        pending = await self.database.pending_auth_incident_alerts()
        self.assertEqual([item.kind for item in pending], ["recovery"])
        await self.database.mark_auth_incident_alert_sent(
            incidents[0].incident_id, "recovery"
        )
        self.assertEqual(await self.database.pending_auth_incident_alerts(), [])

        await self.database.close()
        await self.database.connect()
        persisted = await self.database.get_auth_state()
        self.assertEqual(persisted.status, RESPONSIVE)
        persisted_incidents = await self.database.recent_auth_incidents()
        self.assertEqual(len(persisted_incidents), 1)
        self.assertIsNotNone(persisted_incidents[0].ended_at)

    async def test_unknown_observation_does_not_erase_confirmed_auth_state(self) -> None:
        await self.apply(0, RESPONSIVE)
        await self.apply(1, RESPONSIVE)
        await self.apply(2, UNRESPONSIVE)
        state, transition = await self.apply(3, UNKNOWN)
        self.assertEqual(state.status, RESPONSIVE)
        self.assertEqual(state.observed_status, UNKNOWN)
        self.assertIsNone(state.pending_status)
        self.assertEqual(state.consecutive_failures, 0)
        self.assertIsNone(transition)


class AuthConfigTests(unittest.TestCase):
    def test_new_port_precedence_legacy_fallback_default_and_explicit_disable(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_auth_port(), 3724)
        with mock.patch.dict(
            os.environ, {"OCTOWOW_REALMLIST_PORT": "4000"}, clear=True
        ):
            self.assertEqual(_auth_port(), 4000)
        with mock.patch.dict(
            os.environ,
            {"OCTOWOW_AUTH_PORT": "5000", "OCTOWOW_REALMLIST_PORT": "4000"},
            clear=True,
        ):
            self.assertEqual(_auth_port(), 5000)
        with mock.patch.dict(os.environ, {"OCTOWOW_AUTH_PORT": ""}, clear=True):
            self.assertIsNone(_auth_port())


if __name__ == "__main__":
    unittest.main()
