from __future__ import annotations

import logging
import re
from math import ceil
from typing import TYPE_CHECKING, Any

import discord

from .duration import format_duration

if TYPE_CHECKING:
    from .cogs.moderation import ModerationCog

log = logging.getLogger(__name__)


def _event_timestamp(row: dict[str, Any]) -> int:
    return int(discord.utils.parse_time(str(row["event_at"])).timestamp())


CASE_PREFIXES = {"warning": "W", "timeout": "T", "ban": "B", "note": "N"}
KINDS_BY_PREFIX = {prefix: kind for kind, prefix in CASE_PREFIXES.items()}


def parse_case_id(value: str) -> tuple[str, int] | None:
    """Turn "W-0003", "t12" or "B 7" into ("warning", 3) etc. None if malformed."""
    match = re.fullmatch(r"\s*([WTBNwtbn])\s*-?\s*0*(\d{1,9})\s*", value or "")
    if match is None:
        return None
    return KINDS_BY_PREFIX[match.group(1).upper()], int(match.group(2))


def _case_label(row: dict[str, Any]) -> str:
    prefix = CASE_PREFIXES[str(row["kind"])]
    return f"{prefix}-{int(row['id']):04d}"


class HistoryPageButton(discord.ui.Button):
    def __init__(self, *, direction: int, disabled: bool):
        self.direction = direction
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Previous" if direction < 0 else "Next",
            emoji="◀️" if direction < 0 else "▶️",
            disabled=disabled,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            return
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message(
                "Only the staff member who opened this panel can use these controls.",
                ephemeral=True,
            )
            return
        view.page += self.direction
        embed = await view.render()
        await interaction.response.edit_message(embed=embed, view=view)


class PagedEmbedView(discord.ui.View):
    """Previous/Next over a fixed list of embeds, editing one ephemeral message."""

    def __init__(self, *, owner: discord.Member, pages: list[discord.Embed]):
        super().__init__(timeout=600)
        self.owner_id = owner.id
        self.pages = pages
        self.page = 0
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.clear_items()
        if len(self.pages) <= 1:
            return
        self.add_item(PagedEmbedButton(direction=-1, disabled=self.page <= 0))
        self.add_item(PagedEmbedButton(direction=1, disabled=self.page >= len(self.pages) - 1))

    @property
    def current(self) -> discord.Embed:
        return self.pages[self.page]

    async def flip(self, interaction: discord.Interaction, direction: int) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the staff member who opened this panel can use these controls.",
                ephemeral=True,
            )
            return
        self.page = max(0, min(self.page + direction, len(self.pages) - 1))
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.current, view=self)


class PagedEmbedButton(discord.ui.Button):
    def __init__(self, *, direction: int, disabled: bool):
        self.direction = direction
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Previous" if direction < 0 else "Next",
            emoji="◀️" if direction < 0 else "▶️",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, PagedEmbedView):
            await view.flip(interaction, self.direction)


class ModerationHistoryView(discord.ui.View):
    """Paginated /check panel. Cases are removed with `/check user remove:<case>`."""

    PER_PAGE = 5

    def __init__(
        self,
        *,
        cog: "ModerationCog",
        owner: discord.Member,
        guild: discord.Guild,
        target: discord.abc.User,
    ):
        super().__init__(timeout=600)
        self.cog = cog
        self.bot = cog.bot
        self.owner_id = owner.id
        self.guild = guild
        self.target = target
        self.page = 0
        self._history: list[dict[str, Any]] = []

    async def render(self) -> discord.Embed:
        summary = await self.bot.moderation_database.get_moderation_summary(
            self.guild.id, self.target.id
        )
        self._history = await self.bot.moderation_database.list_moderation_history(
            self.guild.id, self.target.id
        )
        total_pages = max(1, ceil(len(self._history) / self.PER_PAGE))
        self.page = max(0, min(self.page, total_pages - 1))
        start = self.page * self.PER_PAGE
        current = self._history[start : start + self.PER_PAGE]

        embed = discord.Embed(title=f"Moderation history — {self.target}")
        embed.add_field(name="Warnings", value=str(summary["warning_count"]), inline=True)
        embed.add_field(name="Timeouts", value=str(summary["timeout_count"]), inline=True)
        embed.add_field(name="Bans", value=str(summary["ban_count"]), inline=True)
        embed.add_field(name="Notes", value=str(summary["note_count"]), inline=True)
        embed.add_field(
            name="Total timeout issued",
            value=format_duration(summary["total_timeout_seconds"]),
            inline=True,
        )

        now = discord.utils.utcnow()
        if isinstance(self.target, discord.Member):
            until = self.target.timed_out_until
            if until is not None and until > now:
                embed.add_field(
                    name="Currently timed out",
                    value=f"Yes — until <t:{int(until.timestamp())}:F>",
                    inline=False,
                )
            else:
                embed.add_field(name="Currently timed out", value="No", inline=False)
        else:
            # A plain User is not in the server right now (left, kicked or banned).
            embed.add_field(name="In server", value="No — not currently a member", inline=False)

        if not current:
            embed.add_field(
                name="History",
                value="No warning, timeout, ban or note cases are currently recorded in `/check`.",
                inline=False,
            )
        else:
            for row in current:
                reason = str(row.get("reason") or "No reason recorded")
                if len(reason) > 700:
                    reason = reason[:697] + "..."
                ts = _event_timestamp(row)
                label = _case_label(row)
                if row["kind"] == "warning":
                    value = (
                        f"**Date:** <t:{ts}:F>\n"
                        f"**Moderator:** <@{int(row['moderator_id'])}>\n"
                        f"**Reason:** {reason}"
                    )
                    embed.add_field(
                        name=f"⚠️ Warning — {label}", value=value, inline=False
                    )
                elif row["kind"] == "note":
                    value = (
                        f"**Date:** <t:{ts}:F>\n"
                        f"**Moderator:** <@{int(row['moderator_id'])}>\n"
                        f"**Note:** {reason}"
                    )
                    embed.add_field(
                        name=f"📝 Note — {label}", value=value, inline=False
                    )
                elif row["kind"] == "ban":
                    value = (
                        f"**Date:** <t:{ts}:F>\n"
                        f"**Moderator:** <@{int(row['moderator_id'])}>\n"
                        f"**Reason:** {reason}"
                    )
                    unbanned_at = row.get("unbanned_at")
                    if unbanned_at:
                        unban_ts = int(discord.utils.parse_time(str(unbanned_at)).timestamp())
                        value += f"\n**Unbanned:** <t:{unban_ts}:F>"
                    embed.add_field(
                        name=f"🔨 Ban — {label}", value=value, inline=False
                    )
                else:
                    value = (
                        f"**Date:** <t:{ts}:F>\n"
                        f"**Moderator:** <@{int(row['moderator_id'])}>\n"
                        f"**Duration:** {format_duration(int(row.get('duration_seconds') or 0))}\n"
                        f"**Reason:** {reason}"
                    )
                    cleanup_minutes = int(row.get("cleanup_minutes") or 0)
                    if cleanup_minutes > 0:
                        value += (
                            f"\n**Cleanup:** {int(row.get('messages_deleted') or 0)} message(s) "
                            f"from previous {cleanup_minutes}m"
                        )
                    embed.add_field(
                        name=f"⏱️ Timeout — {label}", value=value, inline=False
                    )

        self.clear_items()
        if total_pages > 1:
            self.add_item(
                HistoryPageButton(direction=-1, disabled=self.page <= 0)
            )
            self.add_item(
                HistoryPageButton(direction=1, disabled=self.page >= total_pages - 1)
            )

        footer = f"Page {self.page + 1}/{total_pages} • User ID: {self.target.id}"
        if current:
            footer += " • /check user remove:<case> removes a case"
        embed.set_footer(text=footer)
        return embed


class ReplaceTimeoutConfirmView(discord.ui.View):
    """Confirm replacing a timeout that is still running."""

    def __init__(
        self,
        *,
        cog: "ModerationCog",
        owner: discord.Member,
        guild: discord.Guild,
        target: discord.Member,
        seconds: int,
        reason: str,
    ):
        super().__init__(timeout=60)
        self.cog = cog
        self.bot = cog.bot
        self.owner_id = owner.id
        self.guild = guild
        self.target = target
        self.seconds = seconds
        self.reason = reason

    async def _guard(self, interaction: discord.Interaction) -> discord.Member | None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the staff member who ran the command can use these controls.", ephemeral=True
            )
            return None
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This control only works in a server.", ephemeral=True)
            return None
        perms = await self.bot.moderation_permissions.for_member(interaction.user)
        if not perms.can_timeout or self.seconds > perms.max_timeout_seconds:
            await interaction.response.send_message(
                "You no longer have permission to issue this timeout.", ephemeral=True
            )
            return None
        return interaction.user

    @discord.ui.button(label="Replace timeout", emoji="⏱️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        actor = await self._guard(interaction)
        if actor is None:
            return
        previous = await self.bot.moderation_database.latest_open_timeout(
            self.guild.id, self.target.id
        )
        # The earlier case is voided the same way /untimeout voids one, so the
        # replaced timeout does not also count in /check.
        replaced_id = await self.bot.moderation_database.end_latest_timeout(
            self.guild.id, self.target.id, actor.id,
            f"Replaced by a new timeout from {actor}",
        )
        await interaction.response.edit_message(
            content="Replacing the timeout…", view=None
        )
        try:
            summary = await self.cog.apply_timeout(
                guild=self.guild, actor=actor, user=self.target,
                seconds=self.seconds, reason=self.reason,
                replaced_case_id=replaced_id if replaced_id is not None else (
                    int(previous["id"]) if previous else None
                ),
            )
        except Exception as exc:
            await interaction.edit_original_response(content=str(exc))
            return
        await interaction.edit_original_response(content=summary)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the staff member who ran the command can use these controls.", ephemeral=True
            )
            return
        await interaction.response.edit_message(
            content="Cancelled. The existing timeout is unchanged.", view=None
        )


class ClearHistoryConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        cog: "ModerationCog",
        owner: discord.Member,
        guild: discord.Guild,
        target: discord.abc.User,
    ):
        super().__init__(timeout=60)
        self.cog = cog
        self.bot = cog.bot
        self.owner_id = owner.id
        self.guild = guild
        self.target = target

    async def _guard(self, interaction: discord.Interaction) -> discord.Member | None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the staff member who started this confirmation can use it.",
                ephemeral=True,
            )
            return None
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This control only works in a server.", ephemeral=True)
            return None
        perms = await self.bot.moderation_permissions.for_member(interaction.user)
        if not perms.can_manage_settings:
            await interaction.response.send_message(
                "You no longer have permission to clear moderation history.", ephemeral=True
            )
            return None
        target_error = self.cog._basic_target_error(interaction.user, self.target)
        if target_error:
            await interaction.response.send_message(target_error, ephemeral=True)
            return None
        return interaction.user

    @discord.ui.button(label="Clear entire history", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        actor = await self._guard(interaction)
        if actor is None:
            return
        result = await self.bot.moderation_database.clear_moderation_history(
            self.guild.id, self.target.id
        )
        await interaction.response.edit_message(
            content=(
                f"Cleared {self.target.mention}'s `/check` profile: "
                f"**{result['warnings']} warning(s)**, **{result['timeouts']} timeout(s)**, "
                f"**{result['bans']} ban(s)** and **{result['notes']} note(s)** removed from history.\n"
                "The underlying rows are retained internally for audit. An active Discord timeout, if any, is not lifted."
            ),
            embed=None,
            view=None,
        )

        audit = discord.Embed(title="Moderation history cleared")
        audit.add_field(name="User", value=f"{self.target.mention} (`{self.target.id}`)", inline=False)
        audit.add_field(name="Warnings removed", value=str(result["warnings"]), inline=True)
        audit.add_field(name="Timeouts removed", value=str(result["timeouts"]), inline=True)
        audit.add_field(name="Bans removed", value=str(result["bans"]), inline=True)
        audit.add_field(name="Notes removed", value=str(result["notes"]), inline=True)
        audit.add_field(name="Cleared by", value=actor.mention, inline=True)
        audit.add_field(
            name="Note",
            value="Rows were retained for audit but excluded from `/check` and `/warnings`.",
            inline=False,
        )
        await self.cog._log(self.guild, audit)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the staff member who started this confirmation can use it.", ephemeral=True
            )
            return
        await interaction.response.edit_message(content="History clear cancelled.", embed=None, view=None)
