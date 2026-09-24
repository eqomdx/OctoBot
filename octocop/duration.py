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


MAX_CLEAR_SECONDS = 60 * 60
MAX_CLEAR_MESSAGES = 200


def parse_clear_amount(value: str) -> tuple[str, int]:
    """Read the /clear argument, which is either a message count or a duration.

    Returns ("count", n) for a bare number such as ``10``, or ("seconds", n) for a
    duration such as ``30s``, ``5m`` or ``1h``. Raises DurationError on anything else.
    """
    raw = (value or "").strip().lower().replace(" ", "")
    if not raw:
        raise DurationError("Enter a number of messages (`10`) or a time (`5m`).")
    if raw.isdigit():
        count = int(raw)
        if count <= 0:
            raise DurationError("Enter at least 1 message.")
        if count > MAX_CLEAR_MESSAGES:
            raise DurationError(f"You can clear at most {MAX_CLEAR_MESSAGES} messages at a time.")
        return "count", count
    if raw.endswith(("d", "w")):
        raise DurationError("Time clears are limited to 1 hour. Use `s`, `m` or `h`.")
    seconds = parse_duration(raw, maximum=MAX_CLEAR_SECONDS)
    return "seconds", seconds


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
