from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from octotracker.connectivity import (
    REACHABLE,
    UNKNOWN,
    UNREACHABLE,
    ConnectivityProbe,
    ConnectivityResult,
)
from octotracker.database import Database
from octotracker.status import ONLINE, REALMS, StatusResult


class ConnectivityProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_reachable_closed_port_timeout_and_unconfigured(self) -> None:
        server = await asyncio.start_server(
            lambda reader, writer: writer.close(), "127.0.0.1", 0
        )
        port = server.sockets[0].getsockname()[1]
        try:
            result = await ConnectivityProbe("127.0.0.1", port, 1).probe()
            self.assertEqual(result.status, REACHABLE)
        finally:
            server.close()
            await server.wait_closed()

        result = await ConnectivityProbe("127.0.0.1", port, 1).probe()
        self.assertEqual(result.status, UNREACHABLE)

        with patch(
            "octotracker.connectivity.asyncio.open_connection",
            new=AsyncMock(side_effect=asyncio.TimeoutError),
        ):
            result = await ConnectivityProbe("example.invalid", 1234, 1).probe()
        self.assertEqual(result.status, UNREACHABLE)
        self.assertIn("TimeoutError", result.error)

        with patch("octotracker.connectivity.asyncio.open_connection") as opened:
            result = await ConnectivityProbe("play.octowow.st", None, 1).probe()
        self.assertEqual(result.status, UNKNOWN)
        opened.assert_not_called()


class ConnectivityStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "connectivity.db")
        await self.database.connect()
        self.now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temp_dir.cleanup()

    async def test_connectivity_confirmation_does_not_change_official_state(self) -> None:
        official = StatusResult(
            checked_at=self.now,
            realms={realm: ONLINE for realm in REALMS},
            reachable=True,
        )
        snapshot, _ = await self.database.apply_status_result(official, 3)
        self.assertEqual(snapshot.overall_status, ONLINE)

        reachable = ConnectivityResult(
            self.now, REACHABLE, "127.0.0.1", 1234
        )
        state = await self.database.apply_connectivity_result(reachable, 3)
        self.assertEqual(state.status, REACHABLE)
        for offset in (1, 2):
            state = await self.database.apply_connectivity_result(
                ConnectivityResult(
                    self.now + timedelta(seconds=offset),
                    UNREACHABLE,
                    "127.0.0.1",
                    1234,
                    "failed",
                ),
                3,
            )
            self.assertEqual(state.status, REACHABLE)
        state = await self.database.apply_connectivity_result(
            ConnectivityResult(
                self.now + timedelta(seconds=3),
                UNREACHABLE,
                "127.0.0.1",
                1234,
                "failed",
            ),
            3,
        )
        self.assertEqual(state.status, UNREACHABLE)
        self.assertEqual(
            (await self.database.get_status_snapshot()).overall_status, ONLINE
        )


if __name__ == "__main__":
    unittest.main()
