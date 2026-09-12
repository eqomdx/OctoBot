from __future__ import annotations


MANAGED_COMMANDS = (
    "status",
    "uptime",
    "incidents",
    "announcement",
    "report",
    "reports",
    "radio",
)


def command_access_allowed(
    allowed_role_ids: set[int],
    user_role_ids: set[int],
    *,
    administrator: bool,
) -> bool:
    """Administrators bypass roles; an empty role list intentionally stays open."""
    return administrator or not allowed_role_ids or bool(
        allowed_role_ids & user_role_ids
    )


def command_role_label(role_id: int, known_role_ids: set[int]) -> str:
    if role_id in known_role_ids:
        return f"<@&{role_id}>"
    return f"Deleted role (ID {role_id})"
