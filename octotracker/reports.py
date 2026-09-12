from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


REPORT_REALMS = ("C'Thun", "N'Zoth", "Y'Shaarj", "All Realms")
REPORT_ISSUES = (
    "Can't connect",
    "Disconnecting",
    "High latency",
    "Other",
)
MAX_REPORT_DETAILS = 500


@dataclass(frozen=True, slots=True)
class CommunityReport:
    report_id: int
    guild_id: int
    user_id: int
    realm: str
    issue: str
    details: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ReportGroup:
    realm: str
    issue: str
    report_count: int
    unique_users: int
    latest_at: datetime


@dataclass(frozen=True, slots=True)
class ReportSummary:
    groups: tuple[ReportGroup, ...]
    unique_users: int
    total_reports: int
    active_minutes: int
    threshold: int

    @property
    def degraded(self) -> bool:
        return self.unique_users >= self.threshold


def validate_report(realm: str, issue: str, details: str | None) -> str | None:
    if realm not in REPORT_REALMS:
        raise ValueError("Unknown report realm")
    if issue not in REPORT_ISSUES:
        raise ValueError("Unknown report issue")
    cleaned = details.strip() if details else None
    if cleaned and len(cleaned) > MAX_REPORT_DETAILS:
        raise ValueError(
            f"Report details must be {MAX_REPORT_DETAILS} characters or fewer"
        )
    return cleaned or None
