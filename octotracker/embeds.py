from __future__ import annotations

from datetime import datetime, timezone

import discord

from .announcements import Announcement
from .auth import (
    MALFORMED_RESPONSE,
    RESPONSIVE,
    UNKNOWN as AUTH_UNKNOWN,
    UNRESPONSIVE,
    AuthProbeResult,
)
from .connectivity import REACHABLE, UNKNOWN as CONNECTION_UNKNOWN, UNREACHABLE
from .database import (
    AuthIncident,
    Incident,
    RadioOccurrence,
    ReportIncident,
    UptimeStats,
)
from .radio import AUTODJ, LIVE as RADIO_LIVE, OFFLINE as RADIO_OFFLINE, UNKNOWN as RADIO_UNKNOWN, RadioSnapshot
from .reports import CommunityReport, ReportSummary
from .status import (
    DEGRADED,
    OFFLINE,
    ONLINE,
    PARTIAL,
    UNKNOWN,
    CombinedStatusSnapshot,
    ServiceTransition,
    StatusSnapshot,
)


STATUS_LABELS = {
    ONLINE: "Online",
    DEGRADED: "Degraded",
    OFFLINE: "Offline",
    PARTIAL: "Partial Outage",
    UNKNOWN: "Unknown",
    REACHABLE: "Reachable",
    UNREACHABLE: "Unreachable",
    RESPONSIVE: "Responsive",
    UNRESPONSIVE: "Unresponsive",
    MALFORMED_RESPONSE: "Malformed Response",
    RADIO_LIVE: "Live",
    AUTODJ: "AutoDJ",
    RADIO_OFFLINE: "Offline",
    RADIO_UNKNOWN: "Unknown",
}
STATUS_ICONS = {
    ONLINE: "🟢",
    DEGRADED: "🟡",
    OFFLINE: "🔴",
    PARTIAL: "🟠",
    UNKNOWN: "⚪",
    REACHABLE: "🟢",
    UNREACHABLE: "🔴",
    CONNECTION_UNKNOWN: "⚪",
    RESPONSIVE: "🟢",
    UNRESPONSIVE: "🔴",
    MALFORMED_RESPONSE: "🟠",
    AUTH_UNKNOWN: "⚪",
    RADIO_LIVE: "🔴",
    AUTODJ: "🎵",
    RADIO_OFFLINE: "⚫",
    RADIO_UNKNOWN: "⚪",
}
STATUS_COLORS = {
    ONLINE: discord.Color.green(),
    DEGRADED: discord.Color.gold(),
    OFFLINE: discord.Color.red(),
    PARTIAL: discord.Color.orange(),
    UNKNOWN: discord.Color.light_grey(),
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status.replace("_", " ").title())


def discord_time(value: datetime | None, style: str = "R") -> str:
    if value is None:
        return "Not available"
    return f"<t:{int(value.timestamp())}:{style}>"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "Unknown"
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts or secs:
        parts.append(f"{secs}s")
    return " ".join(parts[:3])


def _elapsed(since: datetime | None) -> float | None:
    if since is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - since).total_seconds())


def _format_uptime_stat(period: UptimeStats) -> str:
    if period.percentage is None:
        return "No data"
    value = f"{period.percentage:.3f}%"
    if period.observed_seconds + 1 < period.window_seconds:
        value += f" ({format_duration(period.observed_seconds)} coverage)"
    return value


def _report_summary_lines(summary: ReportSummary, *, max_groups: int = 8) -> list[str]:
    lines = [
        f"**{group.realm} — {group.issue}:** {group.unique_users} user(s)"
        for group in summary.groups[:max_groups]
    ]
    if len(summary.groups) > max_groups:
        lines.append(f"…and {len(summary.groups) - max_groups} more group(s)")
    return lines


def status_embed(
    snapshot: CombinedStatusSnapshot,
    stats: list[tuple[str, UptimeStats]],
    last_incident: Incident | None,
    confirmation_checks: int = 3,
    *,
    last_auth_incident: AuthIncident | None = None,
    auth_failure_confirmations: int = 3,
    auth_recovery_confirmations: int = 2,
) -> discord.Embed:
    overall = snapshot.overall_status
    official = snapshot.official
    embed = discord.Embed(
        title="OctoBot — Server Status",
        description=(
            f"{STATUS_ICONS.get(overall, '⚪')} Overall: "
            f"**{status_label(overall)}**"
        ),
        color=STATUS_COLORS.get(overall, discord.Color.light_grey()),
        url="https://octowow.st/",
        timestamp=official.checked_at,
    )

    realm_lines: list[str] = []
    for realm in official.realms:
        line = (
            f"{STATUS_ICONS.get(realm.status, '⚪')} **{realm.name}:** "
            f"{status_label(realm.status)}"
        )
        if realm.pending_status:
            line += (
                f" — confirming {status_label(realm.pending_status)} "
                f"({realm.pending_count}/{confirmation_checks})"
            )
        realm_lines.append(line)
    embed.add_field(name="Official realm status", value="\n".join(realm_lines), inline=False)

    connection = snapshot.connectivity
    if connection.port is not None:
        connection_value = (
            f"{STATUS_ICONS.get(connection.status, '⚪')} "
            f"**{status_label(connection.status)}**\n"
            f"`{connection.host}:{connection.port}`"
        )
    else:
        connection_value = f"⚪ **Unknown**\n`{connection.host}` — no verified port configured"
    if connection.pending_status:
        connection_value += (
            f"\nConfirming {status_label(connection.pending_status)} "
            f"({connection.pending_count}/{confirmation_checks})"
        )
    embed.add_field(name="Login endpoint (TCP)", value=connection_value, inline=False)

    authentication = snapshot.authentication
    if authentication is None:
        auth_value = "⚪ **Unknown** — no authentication observation yet"
    elif connection.status == UNREACHABLE:
        auth_value = (
            "⚪ **Unknown** — the TCP endpoint is unreachable\n"
            f"Last confirmed state: **{status_label(authentication.status)}**"
        )
    else:
        auth_value = (
            f"{STATUS_ICONS.get(authentication.status, '⚪')} "
            f"**{status_label(authentication.status)}**"
        )
        if authentication.latency_ms is not None:
            auth_value += f" · {authentication.latency_ms} ms"
        if authentication.reason and authentication.status != RESPONSIVE:
            auth_value += f"\n{authentication.reason}"
        if authentication.pending_status:
            target = (
                auth_recovery_confirmations
                if authentication.pending_status == RESPONSIVE
                else auth_failure_confirmations
            )
            auth_value += (
                f"\nConfirming {status_label(authentication.pending_status)} "
                f"({authentication.pending_count}/{target})"
            )
    embed.add_field(name="Authentication", value=auth_value, inline=False)

    reports = snapshot.reports
    if not reports.groups:
        report_value = (
            f"🟢 No recent connection reports\n"
            f"Last {reports.active_minutes} minutes · threshold {reports.threshold} users"
        )
    else:
        icon = "🟠" if reports.degraded else "🟡"
        heading = (
            f"{icon} **{reports.unique_users} unique user(s)** reporting connection issues"
            if reports.degraded
            else f"{icon} **{reports.unique_users} recent reporting user(s)**"
        )
        report_value = heading + "\n" + "\n".join(_report_summary_lines(reports))
        report_value += (
            f"\nLast {reports.active_minutes} minutes · threshold {reports.threshold} users"
        )
    embed.add_field(name="Community Reports", value=report_value, inline=False)

    elapsed = _elapsed(official.status_since)
    duration_name = (
        "Current official uptime"
        if official.overall_status == ONLINE
        else "Current official interruption"
    )
    embed.add_field(name=duration_name, value=format_duration(elapsed), inline=True)
    stability = []
    for minutes in (10, 20, 30):
        reached = (
            official.overall_status == ONLINE
            and elapsed is not None
            and elapsed >= minutes * 60
        )
        stability.append(f"{'✅' if reached else '⬜'} {minutes}m")
    embed.add_field(name="Stability", value=" · ".join(stability), inline=True)

    if last_incident is None:
        interruption = "No confirmed interruption recorded."
    elif last_incident.ended_at is None:
        interruption = (
            f"Ongoing since {discord_time(last_incident.started_at)} "
            f"({format_duration(last_incident.duration_seconds)})"
        )
    else:
        interruption = (
            f"Started {discord_time(last_incident.started_at)}; recovered "
            f"{discord_time(last_incident.ended_at)} "
            f"({format_duration(last_incident.duration_seconds)})"
        )
    embed.add_field(name="Last official interruption", value=interruption, inline=False)

    if last_auth_incident is None:
        auth_interruption = "No confirmed authentication incident recorded."
    elif last_auth_incident.ended_at is None:
        auth_interruption = (
            f"Ongoing since {discord_time(last_auth_incident.started_at)} "
            f"({format_duration(last_auth_incident.duration_seconds)})"
        )
    else:
        auth_interruption = (
            f"Started {discord_time(last_auth_incident.started_at)}; recovered "
            f"{discord_time(last_auth_incident.ended_at)} "
            f"({format_duration(last_auth_incident.duration_seconds)})"
        )
    embed.add_field(name="Last authentication incident", value=auth_interruption, inline=False)

    embed.add_field(
        name="Official uptime history",
        value="\n".join(
            f"**{label}:** {_format_uptime_stat(period)}" for label, period in stats
        ),
        inline=False,
    )
    source_value = (
        f"Official website: **{'Available' if official.source_ok else 'Unavailable'}**"
        f"\nTCP probe: **{status_label(connection.status)}**"
        f"\nAuthentication probe: **{status_label(authentication.observed_status) if authentication else status_label(AUTH_UNKNOWN)}**"
        f"\nCommunity window: **{reports.active_minutes} minutes**"
    )

    if not official.source_ok and official.last_success_at:
        source_value += f"\nLast official success {discord_time(official.last_success_at)}"

    embed.add_field(
        name="Source availability",
        value=source_value,
        inline=False,
    )

    embed.add_field(
        name="Last checked",
        value=discord_time(official.checked_at),
        inline=True,
    )

    embed.set_footer(
        text=(
            "Realm Online does not guarantee login is possible; authentication "
            "is tested separately without credentials"
        )
    )

    return embed


def uptime_embed(snapshot: StatusSnapshot, stats: list[tuple[str, UptimeStats]]) -> discord.Embed:
    embed = discord.Embed(
        title="OctoWoW official uptime",
        color=STATUS_COLORS.get(snapshot.overall_status, discord.Color.light_grey()),
        timestamp=datetime.now(timezone.utc),
    )
    elapsed = _elapsed(snapshot.status_since)
    if snapshot.overall_status == ONLINE:
        current_name = "Current uptime"
    elif snapshot.overall_status in {PARTIAL, OFFLINE}:
        current_name = "Current interruption"
    else:
        current_name = "Current state duration"
    embed.add_field(name=current_name, value=format_duration(elapsed), inline=False)
    for label, period in stats:
        embed.add_field(name=label, value=_format_uptime_stat(period), inline=True)
    embed.set_footer(text="Confirmed official monitoring time only; partial outages count as downtime")
    return embed


def reports_embed(summary: ReportSummary) -> discord.Embed:
    embed = discord.Embed(
        title="Current OctoWoW community reports",
        color=discord.Color.gold() if summary.degraded else discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    if not summary.groups:
        embed.description = (
            f"🟢 No reports are active in the last {summary.active_minutes} minutes."
        )
    else:
        icon = "🟠" if summary.degraded else "🟡"
        embed.description = (
            f"{icon} **{summary.unique_users} unique reporting user(s)** in the last "
            f"{summary.active_minutes} minutes."
        )
        for group in summary.groups[:20]:
            embed.add_field(
                name=f"{group.realm} — {group.issue}",
                value=(
                    f"**{group.unique_users} user(s)** · {group.report_count} active report(s)\n"
                    f"Latest {discord_time(group.latest_at)}"
                ),
                inline=False,
            )
    embed.set_footer(
        text=f"Degraded at {summary.threshold} unique reporting users; reports never declare Offline"
    )
    return embed


def reports_details_embed(reports: list[CommunityReport], active_minutes: int) -> discord.Embed:
    embed = discord.Embed(
        title="Active OctoWoW report details",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    if not reports:
        embed.description = f"No reports are active in the last {active_minutes} minutes."
        return embed

    lines: list[str] = []
    for report in reports[:30]:
        detail = f' — “{report.details[:180]}”' if report.details else ""
        lines.append(
            f"**{report.realm} — {report.issue}** · <@{report.user_id}> · "
            f"{discord_time(report.updated_at)}{detail}"
        )
    if len(reports) > 30:
        lines.append(f"…and {len(reports) - 30} more active report(s).")
    text = "\n".join(lines)
    embed.description = text[:4096]
    embed.set_footer(text=f"Moderator detail view · active window {active_minutes} minutes")
    return embed


def incidents_embed(
    incidents: list[Incident],
    auth_incidents: list[AuthIncident] | None = None,
    report_incidents: list[ReportIncident] | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="Recent OctoWoW incidents",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    auth_incidents = auth_incidents or []
    report_incidents = report_incidents or []
    if not incidents and not auth_incidents and not report_incidents:
        embed.description = "No confirmed incidents have been recorded yet."
        return embed

    combined = sorted(
        [(item.started_at, "official", item) for item in incidents]
        + [(item.started_at, "auth", item) for item in auth_incidents]
        + [(item.started_at, "reports", item) for item in report_incidents],
        key=lambda entry: entry[0],
        reverse=True,
    )
    for _, category, incident in combined[:10]:
        state = "Ongoing" if incident.ended_at is None else "Recovered"
        if category == "official":
            status = incident.worst_status
            name = f"Official incident #{incident.incident_id}"
            detail = status_label(status)
        elif category == "auth":
            name = f"Authentication incident #{incident.incident_id}"
            detail = status_label(incident.latest_status)
        else:
            name = f"Community report incident #{incident.incident_id}"
            detail = f"Peak {incident.peak_unique_users} reporting users"
        value = (
            f"**{state}** · {detail}\n"
            f"Started {discord_time(incident.started_at)} · "
            f"Duration {format_duration(incident.duration_seconds)}"
        )
        if incident.ended_at:
            value += f"\nRecovered {discord_time(incident.ended_at)}"
        embed.add_field(name=name, value=value, inline=False)
    return embed


def auth_degraded_embed(incident: AuthIncident) -> discord.Embed:
    embed = discord.Embed(
        title="🔴 OctoWoW authentication degraded",
        description=(
            "The login endpoint is not returning a normal Vanilla authentication response. "
            "Players may become stuck at Authenticating."
        ),
        color=discord.Color.red(),
        timestamp=incident.started_at,
    )
    embed.add_field(name="Authentication", value=status_label(incident.latest_status), inline=True)
    embed.add_field(name="TCP", value=status_label(incident.tcp_status), inline=True)
    if incident.reason:
        embed.add_field(name="Probe result", value=incident.reason, inline=False)
    embed.set_footer(text="No account credentials or login proof were sent")
    return embed


def auth_recovery_embed(incident: AuthIncident) -> discord.Embed:
    recovered_at = incident.ended_at or datetime.now(timezone.utc)
    embed = discord.Embed(
        title="🟢 OctoWoW authentication recovered",
        color=discord.Color.green(),
        timestamp=recovered_at,
    )
    embed.add_field(name="Interruption", value=format_duration(incident.duration_seconds), inline=True)
    embed.add_field(name="Recovery time", value=discord_time(recovered_at, "F"), inline=True)
    if incident.latency_ms is not None:
        embed.add_field(name="Response latency", value=f"{incident.latency_ms} ms")
    embed.set_footer(text="Authentication protocol response restored")
    return embed


def authcheck_embed(result: AuthProbeResult) -> discord.Embed:
    color = (
        discord.Color.green()
        if result.auth_status == RESPONSIVE
        else discord.Color.red()
        if result.tcp_status == UNREACHABLE or result.auth_status == UNRESPONSIVE
        else discord.Color.orange()
        if result.auth_status == MALFORMED_RESPONSE
        else discord.Color.light_grey()
    )
    embed = discord.Embed(
        title="OctoWoW authentication diagnostic",
        description=(
            "Immediate one-off probe. This result does not alter monitoring "
            "history, incidents, or alerts."
        ),
        color=color,
        timestamp=result.checked_at,
    )
    endpoint = f"{result.host}:{result.port}" if result.port else result.host
    embed.add_field(name="Endpoint", value=f"`{endpoint}`", inline=False)
    embed.add_field(name="TCP", value=status_label(result.tcp_status), inline=True)
    embed.add_field(name="Authentication", value=status_label(result.auth_status), inline=True)
    embed.add_field(
        name="Latency",
        value=f"{result.latency_ms} ms" if result.latency_ms is not None else "N/A",
        inline=True,
    )
    if result.reason:
        embed.add_field(name="Result", value=result.reason, inline=False)
    embed.set_footer(text="Credential-free challenge only; no login proof sent")
    return embed


def outage_embed(snapshot: StatusSnapshot, transition: ServiceTransition) -> discord.Embed:
    affected = [realm.name for realm in snapshot.realms if realm.status == OFFLINE]
    embed = discord.Embed(
        title=f"{STATUS_ICONS.get(transition.new_status, '🔴')} OctoWoW outage confirmed",
        description=(
            "Affected realm(s): " + ", ".join(affected)
            if affected
            else "A service interruption has been confirmed."
        ),
        color=STATUS_COLORS.get(transition.new_status, discord.Color.red()),
        timestamp=transition.changed_at,
    )
    embed.add_field(name="Overall status", value=status_label(transition.new_status), inline=True)
    embed.add_field(name="Confirmed", value=discord_time(transition.changed_at), inline=True)
    return embed


def recovery_embed(transition: ServiceTransition) -> discord.Embed:
    embed = discord.Embed(
        title="🟢 OctoWoW service recovered",
        color=discord.Color.green(),
        timestamp=transition.changed_at,
    )
    embed.add_field(name="Downtime", value=format_duration(transition.duration_seconds), inline=True)
    embed.add_field(name="Recovery time", value=discord_time(transition.changed_at, "F"), inline=True)
    return embed


def report_degraded_embed(
    incident: ReportIncident,
    summary: ReportSummary,
    snapshot: CombinedStatusSnapshot,
) -> discord.Embed:
    embed = discord.Embed(
        title="🟠 Community connection issues reported",
        description=(
            f"**{summary.unique_users} users** have reported connection problems "
            f"in the last {summary.active_minutes} minutes."
        ),
        color=discord.Color.orange(),
        timestamp=incident.started_at,
    )
    if summary.groups:
        embed.add_field(
            name="Reports",
            value="\n".join(_report_summary_lines(summary)),
            inline=False,
        )
    embed.add_field(
        name="Official realm status",
        value=status_label(snapshot.official.overall_status),
        inline=True,
    )
    if snapshot.authentication is not None:
        embed.add_field(
            name="Authentication",
            value=status_label(snapshot.authentication.status),
            inline=True,
        )
    embed.set_footer(text="Community reports can mark service Degraded, never Offline")
    return embed


def report_recovery_embed(incident: ReportIncident) -> discord.Embed:
    recovered_at = incident.ended_at or datetime.now(timezone.utc)
    embed = discord.Embed(
        title="🟢 Community reports cleared",
        description="Recent connection reports have dropped below the degraded threshold.",
        color=discord.Color.green(),
        timestamp=recovered_at,
    )
    embed.add_field(name="Peak reporters", value=str(incident.peak_unique_users), inline=True)
    embed.add_field(name="Duration", value=format_duration(incident.duration_seconds), inline=True)
    return embed


def radio_live_embed(occurrence: RadioOccurrence, public_url: str) -> discord.Embed:
    presenter = occurrence.presenter or occurrence.title
    embed = discord.Embed(
        title="📻 Booty Bay Pirate Radio — Now Live",
        description=f"🔴 **LIVE NOW**\n**{occurrence.title}**\nwith **{presenter}**",
        url=public_url,
        color=discord.Color.red(),
        timestamp=occurrence.detected_live_at,
    )
    embed.add_field(
        name="Started",
        value=discord_time(occurrence.scheduled_start or occurrence.detected_live_at, "F"),
        inline=True,
    )
    if occurrence.scheduled_end:
        embed.add_field(name="Scheduled end", value=discord_time(occurrence.scheduled_end, "t"), inline=True)
    if occurrence.description:
        embed.add_field(name="About this show", value=occurrence.description[:1024], inline=False)
    embed.add_field(name="Listen", value=f"[Booty Bay Pirate Radio]({public_url})", inline=False)
    if occurrence.artwork_url and occurrence.artwork_url.startswith(("http://", "https://")):
        embed.set_thumbnail(url=occurrence.artwork_url)
    embed.set_footer(text="Tune in through the in-game radio or the web player")
    return embed


def radio_status_embed(snapshot: RadioSnapshot) -> discord.Embed:
    color = (
        discord.Color.red()
        if snapshot.state == RADIO_LIVE
        else discord.Color.blurple()
        if snapshot.state == AUTODJ
        else discord.Color.dark_grey()
        if snapshot.state == RADIO_OFFLINE
        else discord.Color.light_grey()
    )
    embed = discord.Embed(
        title="📻 Booty Bay Pirate Radio",
        url=snapshot.public_url,
        color=color,
        timestamp=snapshot.checked_at,
    )
    if not snapshot.source_ok:
        embed.description = "⚪ **Radio status unavailable**\nThe radio API could not be read right now."
        if snapshot.error:
            embed.add_field(name="Source", value=snapshot.error[:1024], inline=False)
        return embed

    if snapshot.state == RADIO_LIVE and snapshot.current_show is not None:
        show = snapshot.current_show
        presenter = show.presenter or "Live DJ"
        embed.description = f"🔴 **LIVE NOW**\n**{show.title}**\nwith **{presenter}**"
        if show.scheduled_start:
            embed.add_field(name="Started", value=discord_time(show.scheduled_start), inline=True)
        if show.scheduled_end:
            embed.add_field(name="Ends", value=discord_time(show.scheduled_end, "t"), inline=True)
    elif snapshot.state == AUTODJ:
        embed.description = "🎵 **AutoDJ is playing**"
        if snapshot.current_track:
            embed.add_field(name="Now playing", value=snapshot.current_track[:1024], inline=False)
    elif snapshot.state == RADIO_OFFLINE:
        embed.description = "⚫ **Station offline**"
    else:
        embed.description = "⚪ **Station state unknown**"

    if snapshot.listeners is not None:
        embed.add_field(name="Listeners", value=str(snapshot.listeners), inline=True)

    if snapshot.schedule_available:
        if snapshot.upcoming:
            lines = []
            for show in snapshot.upcoming[:5]:
                when = discord_time(show.scheduled_start, "f")
                presenter = f" — {show.presenter}" if show.presenter else ""
                lines.append(f"**{show.title}**{presenter}\n{when}")
            embed.add_field(name="Upcoming shows", value="\n\n".join(lines), inline=False)
        else:
            embed.add_field(name="Upcoming shows", value="No upcoming live shows are currently scheduled.", inline=False)
    else:
        message = "Upcoming schedule is unavailable from the public radio API."
        if snapshot.schedule_error:
            message += f"\n{snapshot.schedule_error}"
        embed.add_field(name="Upcoming shows", value=message[:1024], inline=False)

    embed.add_field(name="Listen", value=f"[Open Booty Bay Pirate Radio]({snapshot.public_url})", inline=False)
    return embed


def help_embed(
    commands: list[tuple[str, str]],
    command_channel: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="🐙 OctoBot Help",
        description=f"Use these commands in {command_channel}:",
        color=discord.Color.blurple(),
    )
    for name, description in commands:
        embed.add_field(name=f"/{name}", value=description, inline=False)
    embed.set_footer(text="Only commands you can currently use are shown")
    return embed


def split_announcement_body(text: str, max_length: int = 3_500) -> list[str]:
    """Split at paragraph/whitespace boundaries, falling back to Unicode-safe slices."""
    if not text:
        return []
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > max_length:
        boundary = remaining.rfind("\n\n", 0, max_length + 1)
        consume = 2
        if boundary < max_length // 3:
            boundary = remaining.rfind("\n", 0, max_length + 1)
            consume = 1
        if boundary < max_length // 3:
            boundary = remaining.rfind(" ", 0, max_length + 1)
            consume = 1
        if boundary <= 0:
            boundary = max_length
            consume = 0
        chunks.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary + consume :].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def announcement_embeds(announcement: Announcement) -> list[discord.Embed]:
    body = (
        announcement.body
        or announcement.preview
        or "The full post could not be fetched. Open the official forum topic to read it."
    )
    pages = split_announcement_body(body) or [body]
    embeds: list[discord.Embed] = []
    for index, page in enumerate(pages, start=1):
        embed = discord.Embed(
            title=announcement.title[:256],
            url=announcement.url,
            description=page,
            color=discord.Color.blurple(),
        )
        if announcement.author:
            embed.set_author(name=announcement.author[:256])
        if announcement.published_at:
            try:
                value = announcement.published_at.replace("Z", "+00:00")
                embed.timestamp = datetime.fromisoformat(value)
            except ValueError:
                if index == 1:
                    embed.add_field(name="Published", value=announcement.published_at, inline=False)
        if index == len(pages):
            embed.add_field(
                name="Official topic",
                value=f"[Open on the OctoWoW forum]({announcement.url})",
                inline=False,
            )
        embed.set_footer(
            text=(
                f"Official OctoWoW announcement · Topic {announcement.topic_id} · "
                f"Page {index}/{len(pages)}"
            )
        )
        embeds.append(embed)
    return embeds


def announcement_embed(announcement: Announcement) -> discord.Embed:
    """Backward-compatible single-page helper."""
    return announcement_embeds(announcement)[0]

