from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _discord_id(name: str, *, required: bool = False) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        if required:
            raise RuntimeError(f"{name} is missing from .env")
        return None

    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a Discord numeric ID") from exc

    if parsed <= 0:
        raise RuntimeError(f"{name} must be a positive Discord numeric ID")
    return parsed


def _positive_int(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a whole number") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return parsed


def _optional_port(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a whole number") from exc
    if not 1 <= parsed <= 65_535:
        raise RuntimeError(f"{name} must be between 1 and 65535")
    return parsed


def _auth_port() -> int | None:
    """Prefer the V3 setting, fall back to V2, then the Vanilla default."""
    if "OCTOWOW_AUTH_PORT" in os.environ:
        return _optional_port("OCTOWOW_AUTH_PORT")
    legacy = _optional_port("OCTOWOW_REALMLIST_PORT")
    return legacy if legacy is not None else 3724


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class Config:
    token: str = field(repr=False)
    guild_id: int
    status_channel_id: int | None
    alert_channel_id: int | None
    announcement_channel_id: int | None
    status_poll_seconds: int = 30
    announcement_poll_seconds: int = 90
    confirmation_checks: int = 3
    request_timeout_seconds: int = 15
    realmlist_host: str = "play.octowow.st"
    realmlist_port: int | None = None
    connectivity_timeout_seconds: int = 5
    auth_port: int | None = 3724
    auth_probe_timeout_seconds: int = 5
    auth_failure_confirmations: int = 3
    auth_recovery_confirmations: int = 2
    radio_enabled: bool = True
    radio_base_url: str = "https://radio.octowow.st"
    radio_station_shortcode: str = "booty_bay_pirate_radio"
    radio_channel_id: int | None = None
    radio_ping_role_id: int | None = None
    radio_poll_seconds: int = 30
    radio_go_live_confirmations: int = 2
    bot_commands_channel_id: int | None = None
    log_channel_id: int | None = None
    helper_role_id: int | None = None
    moderator_role_id: int | None = None
    admin_role_id: int | None = None
    report_active_minutes: int = 10
    report_degraded_threshold: int = 3
    database_path: Path = PROJECT_ROOT / "data" / "octobot.db"

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv(PROJECT_ROOT / ".env", override=True)

        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise RuntimeError("DISCORD_TOKEN is missing from .env")

        guild_id = _discord_id("DISCORD_GUILD_ID", required=True)
        assert guild_id is not None

        return cls(
            token=token,
            guild_id=guild_id,
            status_channel_id=_discord_id("DISCORD_STATUS_CHANNEL_ID"),
            alert_channel_id=_discord_id("DISCORD_ALERT_CHANNEL_ID"),
            announcement_channel_id=_discord_id(
                "DISCORD_ANNOUNCEMENT_CHANNEL_ID"
            ),
            status_poll_seconds=_positive_int("STATUS_POLL_SECONDS", 30),
            announcement_poll_seconds=_positive_int(
                "ANNOUNCEMENT_POLL_SECONDS", 90
            ),
            confirmation_checks=_positive_int("CONFIRMATION_CHECKS", 3),
            request_timeout_seconds=_positive_int(
                "REQUEST_TIMEOUT_SECONDS", 15
            ),
            realmlist_host=(
                os.getenv("OCTOWOW_REALMLIST_HOST", "play.octowow.st").strip()
                or "play.octowow.st"
            ),
            realmlist_port=_optional_port("OCTOWOW_REALMLIST_PORT"),
            connectivity_timeout_seconds=_positive_int(
                "CONNECTIVITY_TIMEOUT_SECONDS", 5
            ),
            auth_port=_auth_port(),
            auth_probe_timeout_seconds=_positive_int(
                "AUTH_PROBE_TIMEOUT_SECONDS", 5
            ),
            auth_failure_confirmations=_positive_int(
                "AUTH_FAILURE_CONFIRMATIONS", 3
            ),
            auth_recovery_confirmations=_positive_int(
                "AUTH_RECOVERY_CONFIRMATIONS", 2
            ),
            radio_enabled=_boolean("RADIO_ENABLED", True),
            radio_base_url=(
                os.getenv("RADIO_BASE_URL", "https://radio.octowow.st").strip()
                or "https://radio.octowow.st"
            ),
            radio_station_shortcode=(
                os.getenv(
                    "RADIO_STATION_SHORTCODE", "booty_bay_pirate_radio"
                ).strip()
                or "booty_bay_pirate_radio"
            ),
            radio_channel_id=_discord_id("DISCORD_RADIO_CHANNEL_ID"),
            radio_ping_role_id=_discord_id("DISCORD_RADIO_PING_ROLE_ID"),
            radio_poll_seconds=_positive_int("RADIO_POLL_SECONDS", 30),
            radio_go_live_confirmations=_positive_int(
                "RADIO_GO_LIVE_CONFIRMATIONS", 2
            ),
            # Kept only for backward compatibility with older OctoTracker .env files.
            # OctoBot commands are intentionally usable in every accessible channel.
            bot_commands_channel_id=_discord_id(
                "DISCORD_BOT_COMMANDS_CHANNEL_ID"
            ),
            log_channel_id=_discord_id("DISCORD_LOG_CHANNEL_ID"),
            helper_role_id=_discord_id("DISCORD_HELPER_ROLE_ID"),
            moderator_role_id=_discord_id("DISCORD_MODERATOR_ROLE_ID"),
            admin_role_id=_discord_id("DISCORD_ADMIN_ROLE_ID"),
            report_active_minutes=_positive_int("REPORT_ACTIVE_MINUTES", 10),
            report_degraded_threshold=_positive_int(
                "REPORT_DEGRADED_THRESHOLD", 3
            ),
            database_path=Path(
                os.getenv("DATABASE_PATH", str(PROJECT_ROOT / "data" / "octobot.db"))
            ).expanduser(),
        )
