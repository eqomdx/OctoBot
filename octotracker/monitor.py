from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone

import discord

from .announcements import AnnouncementClient
from .auth import AuthProbe, AuthProbeResult
from .config import Config
from .database import (
    AuthIncidentNotification,
    Database,
    IncidentNotification,
    ReportIncidentNotification,
)
from .embeds import (
    announcement_embeds,
    authcheck_embed,
    auth_degraded_embed,
    auth_recovery_embed,
    outage_embed,
    recovery_embed,
    report_degraded_embed,
    report_recovery_embed,
    status_embed,
)
from .status import (
    CombinedStatusSnapshot,
    ServiceTransition,
    StatusClient,
    StatusSnapshot,
    combine_status,
)


LOGGER = logging.getLogger(__name__)


class Monitor:
    def __init__(
        self,
        bot: discord.Client,
        config: Config,
        database: Database,
        status_client: StatusClient,
        auth_probe: AuthProbe,
        announcement_client: AnnouncementClient,
    ):
        self.bot = bot
        self.config = config
        self.database = database
        self.status_client = status_client
        self.auth_probe = auth_probe
        self.announcement_client = announcement_client
        self._tasks: list[asyncio.Task[None]] = []
        self._status_lock = asyncio.Lock()
        self._announcement_lock = asyncio.Lock()
        self._last_health_signature = None

    def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._status_loop(), name="status-monitor"),
            asyncio.create_task(
                self._announcement_loop(), name="announcement-monitor"
            ),
        ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    async def _status_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self.check_status()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Unexpected error in status monitor")
            await asyncio.sleep(self.config.status_poll_seconds)
    async def _send_auth_diagnostic(
        self,
        result: AuthProbeResult,
    ) -> None:
        channel_id = self.config.alert_channel_id or self.config.status_channel_id

        if channel_id is None:
            return

        await self._send_embed(
            channel_id,
            authcheck_embed(result),
        )

    @staticmethod
    def _health_signature(snapshot: CombinedStatusSnapshot) -> tuple[object, ...]:
        """Return only meaningful health state, excluding volatile latency/timestamps."""
        return (
            snapshot.overall_status,
            snapshot.official.source_ok,
            tuple((realm.name, realm.status) for realm in snapshot.official.realms),
            snapshot.connectivity.status,
            snapshot.authentication.status if snapshot.authentication else None,
            snapshot.reports.degraded,
        )

    async def _send_diagnostic_if_health_changed(
        self, snapshot: CombinedStatusSnapshot
    ) -> None:
        """Establish a startup baseline, then diagnose subsequent health changes."""
        health_signature = self._health_signature(snapshot)
        health_changed = (
            self._last_health_signature is not None
            and health_signature != self._last_health_signature
        )
        self._last_health_signature = health_signature

        if not health_changed:
            return

        diagnostic = await self.probe_authentication()
        await self._send_auth_diagnostic(diagnostic)

    async def _announcement_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self.check_announcements()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Unexpected error in announcement monitor")
            await asyncio.sleep(self.config.announcement_poll_seconds)

    async def check_status(self) -> CombinedStatusSnapshot:
        async with self._status_lock:
            status_result, auth_result = await asyncio.gather(
                self.status_client.fetch(), self.auth_probe.probe()
            )
            official, _ = await self.database.apply_status_result(
                status_result, self.config.confirmation_checks
            )
            connectivity = await self.database.apply_connectivity_result(
                auth_result.connectivity_result(), self.config.confirmation_checks
            )
            authentication, _ = await self.database.apply_auth_result(
                auth_result,
                self.config.auth_failure_confirmations,
                self.config.auth_recovery_confirmations,
            )
            reports = await self.database.report_summary(
                self.config.guild_id,
                self.config.report_active_minutes,
                self.config.report_degraded_threshold,
            )
            await self.database.apply_report_alert_state(
                self.config.guild_id, reports
            )
            snapshot = combine_status(
                official, connectivity, reports, authentication
            )
            await self._update_status_message(snapshot)
            await self._send_diagnostic_if_health_changed(snapshot)
            await self._send_pending_incident_alerts(official)
            await self._send_pending_auth_incident_alerts()
            await self._send_pending_report_incident_alerts(snapshot)
            return snapshot

    async def probe_authentication(self) -> AuthProbeResult:
        """Run a diagnostic probe without changing confirmed or historical state."""
        return await self.auth_probe.probe()

    async def refresh_status_from_database(self) -> CombinedStatusSnapshot:
        """Refresh the persistent dashboard using stored state without probing upstreams."""
        async with self._status_lock:
            official, connectivity, authentication = await asyncio.gather(
                self.database.get_status_snapshot(),
                self.database.get_connectivity_state(),
                self.database.get_auth_state(),
            )
            reports = await self.database.report_summary(
                self.config.guild_id,
                self.config.report_active_minutes,
                self.config.report_degraded_threshold,
            )
            await self.database.apply_report_alert_state(
                self.config.guild_id, reports
            )
            snapshot = combine_status(
                official, connectivity, reports, authentication
            )

            await self._update_status_message(snapshot)
            await self._send_diagnostic_if_health_changed(snapshot)

            await self._send_pending_incident_alerts(official)
            await self._send_pending_auth_incident_alerts()
            await self._send_pending_report_incident_alerts(snapshot)

            return snapshot

    async def make_status_embed(
        self, snapshot: CombinedStatusSnapshot
    ) -> discord.Embed:
        stats = await asyncio.gather(
            self.database.uptime_stats(timedelta(days=1)),
            self.database.uptime_stats(timedelta(days=7)),
            self.database.uptime_stats(timedelta(days=30)),
        )
        incidents, auth_incidents = await asyncio.gather(
            self.database.recent_incidents(limit=1),
            self.database.recent_auth_incidents(limit=1),
        )
        return status_embed(
            snapshot,
            list(zip(("Last 24 hours", "Last 7 days", "Last 30 days"), stats)),
            incidents[0] if incidents else None,
            self.config.confirmation_checks,
            last_auth_incident=auth_incidents[0] if auth_incidents else None,
            auth_failure_confirmations=self.config.auth_failure_confirmations,
            auth_recovery_confirmations=self.config.auth_recovery_confirmations,
        )

    async def check_announcements(self) -> None:
        async with self._announcement_lock:
            topics = await self.announcement_client.fetch_topics()
            if not topics:
                return

            if not await self.database.announcements_initialized():
                await self.database.seed_announcements(topics)
                LOGGER.info(
                    "Initialized %s existing announcement topic(s) as seen",
                    len(topics),
                )
                return

            known_ids = await self.database.known_topic_ids()
            new_topics = [topic for topic in topics if topic.topic_id not in known_ids]
            for topic in sorted(new_topics, key=lambda item: item.topic_id):
                enriched = await self.announcement_client.enrich(topic)
                await self.database.store_announcement(enriched)

            if self.config.announcement_channel_id is None:
                return

            for announcement in await self.database.pending_announcements():
                pages = announcement_embeds(announcement)
                progress = await self.database.announcement_delivery_progress(
                    announcement.topic_id
                )
                delivery_failed = False
                for page_number, embed in enumerate(
                    pages[progress:], start=progress + 1
                ):
                    if not await self._send_embed(
                        self.config.announcement_channel_id, embed
                    ):
                        delivery_failed = True
                        break
                    await self.database.mark_announcement_page_sent(
                        announcement.topic_id, page_number
                    )
                if delivery_failed:
                    break
                await self.database.mark_announcement_sent(announcement.topic_id)

    async def latest_announcement(self):
        await self.check_announcements()
        announcement = await self.database.latest_announcement()
        if announcement is None or announcement.body:
            return announcement
        enriched = await self.announcement_client.enrich(announcement)
        await self.database.store_announcement(enriched)
        return enriched

    async def _get_channel(self, channel_id: int) -> object | None:
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            LOGGER.exception("Could not access Discord channel %s", channel_id)
            return None

    async def _send_embed(self, channel_id: int, embed: discord.Embed) -> bool:
        channel = await self._get_channel(channel_id)
        if channel is None or not hasattr(channel, "send"):
            LOGGER.error("Discord channel %s cannot receive messages", channel_id)
            return False
        try:
            await channel.send(embed=embed)
            return True
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Could not send a message to channel %s", channel_id)
            return False

    async def _update_status_message(
        self, snapshot: CombinedStatusSnapshot
    ) -> None:
        channel_id = self.config.status_channel_id
        if channel_id is None:
            return
        channel = await self._get_channel(channel_id)
        if channel is None or not hasattr(channel, "send"):
            LOGGER.error("Status channel %s cannot receive messages", channel_id)
            return

        embed = await self.make_status_embed(snapshot)
        metadata_key = f"status_message_id:{channel_id}"
        message_id = await self.database.get_metadata(metadata_key)

        if message_id and hasattr(channel, "fetch_message"):
            try:
                message = await channel.fetch_message(int(message_id))
                await message.edit(embed=embed)
                return
            except discord.NotFound:
                LOGGER.warning("Stored status message no longer exists; recreating it")
            except (discord.Forbidden, discord.HTTPException, ValueError):
                LOGGER.exception("Could not update the persistent status message")
                return

        try:
            message = await channel.send(embed=embed)
            await self.database.set_metadata(metadata_key, str(message.id))
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Could not create the persistent status message")

    async def _send_pending_incident_alerts(
        self, snapshot: StatusSnapshot
    ) -> None:
        channel_id = self.config.alert_channel_id
        if channel_id is None:
            return

        for notification in await self.database.pending_incident_alerts():
            embed = self._incident_embed(snapshot, notification)
            if not await self._send_embed(channel_id, embed):
                break
            await self.database.mark_incident_alert_sent(
                notification.incident.incident_id, notification.kind
            )

    async def _send_pending_auth_incident_alerts(self) -> None:
        channel_id = self.config.alert_channel_id
        if channel_id is None:
            return

        for notification in await self.database.pending_auth_incident_alerts():
            embed = self._auth_incident_embed(notification)
            if not await self._send_embed(channel_id, embed):
                break
            await self.database.mark_auth_incident_alert_sent(
                notification.incident.incident_id, notification.kind
            )

    async def _send_pending_report_incident_alerts(
        self, snapshot: CombinedStatusSnapshot
    ) -> None:
        channel_id = self.config.alert_channel_id
        if channel_id is None:
            return

        for notification in await self.database.pending_report_incident_alerts(
            self.config.guild_id
        ):
            embed = self._report_incident_embed(notification, snapshot)
            if not await self._send_embed(channel_id, embed):
                break
            await self.database.mark_report_incident_alert_sent(
                notification.incident.incident_id, notification.kind
            )

    @staticmethod
    def _report_incident_embed(
        notification: ReportIncidentNotification,
        snapshot: CombinedStatusSnapshot,
    ) -> discord.Embed:
        if notification.kind == "degraded":
            return report_degraded_embed(
                notification.incident, snapshot.reports, snapshot
            )
        return report_recovery_embed(notification.incident)

    @staticmethod
    def _incident_embed(
        snapshot: StatusSnapshot, notification: IncidentNotification
    ) -> discord.Embed:
        incident = notification.incident
        if notification.kind == "outage":
            transition = ServiceTransition(
                kind="outage",
                old_status="online",
                new_status=incident.initial_status,
                changed_at=incident.started_at,
                incident_id=incident.incident_id,
            )
            return outage_embed(snapshot, transition)

        ended_at = incident.ended_at or datetime.now(timezone.utc)
        transition = ServiceTransition(
            kind="recovery",
            old_status=incident.worst_status,
            new_status="online",
            changed_at=ended_at,
            incident_id=incident.incident_id,
            duration_seconds=incident.duration_seconds,
        )
        return recovery_embed(transition)

    @staticmethod
    def _auth_incident_embed(
        notification: AuthIncidentNotification,
    ) -> discord.Embed:
        if notification.kind == "degraded":
            return auth_degraded_embed(notification.incident)
        return auth_recovery_embed(notification.incident)
