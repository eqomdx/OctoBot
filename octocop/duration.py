from __future__ import annotations

import re

MAX_TIMEOUT_SECONDS = 28 * 24 * 60 * 60

_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}

_DURATION_RE = re.compile(r"(\d+)([smhdw])", re.IGNORECASE)


class DurationError(ValueError):
    pass


def parse_duration(value: str, *, maximum: int | None = MAX_TIMEOUT_SECONDS) -> int:
    """Parse Discord-friendly durations such as 30s, 15m, 1h30m, 2d, or 1w3d."""
    raw = value.strip().lower().replace(" ", "")
    if not raw:
        raise DurationError("Duration cannot be empty.")

    total = 0
    cursor = 0
    matches = list(_DURATION_RE.finditer(raw))
    if not matches:
        raise DurationError("Use a duration like `30s`, `15m`, `1h`, `2d`, `1w`, or `1h30m`.")

    for match in matches:
        if match.start() != cursor:
            raise DurationError("Invalid duration format. Use only S, M, H, D, and W units.")
        amount = int(match.group(1))
        if amount <= 0:
            raise DurationError("Duration values must be greater than zero.")
        total += amount * _UNIT_SECONDS[match.group(2)]
        cursor = match.end()

    if cursor != len(raw):
        raise DurationError("Invalid duration format. Use only S, M, H, D, and W units.")
    if total <= 0:
        raise DurationError("Duration must be greater than zero.")
    if maximum is not None and total > maximum:
        raise DurationError(f"Duration cannot exceed {format_duration(maximum)}.")
    return total


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds == 0:
        return "0s"

    parts: list[str] = []
    for suffix, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        amount, seconds = divmod(seconds, size)
        if amount:
            parts.append(f"{amount}{suffix}")
    return " ".join(parts)
