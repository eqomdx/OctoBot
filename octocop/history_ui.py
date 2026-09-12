from __future__ import annotations

import logging
from math import ceil
from typing import TYPE_CHECKING, Any

import discord

from .duration import format_duration

if TYPE_CHECKING:
    from .cogs.moderation import ModerationCog

log = logging.getLogger(__name__)


def _event_timestamp(row: dict[str, Any]) -> int:
    return int(discord.utils.parse_time(str(row["event_at"])).timestamp())


def _case_label(row: dict[str, Any]) -> str:
    prefix = "W" if row["kind"] == "warning" else "T"
    return f"{prefix}-{int(row['id']):04d}"


class HistoryCaseButton(discord.ui.Button):
    def __init__(self, row: dict[str, Any]):
        self.case_kind = str(row["kind"])
        self.case_id = int(row["id"])
        super().__init__(
            style=discord.ButtonStyle.danger,
            label=_case_label(row),
            emoji="❌",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            return
        await view.remove_case(interaction, self.case_kind, self.case_id)


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


class ModerationHistoryView(discord.ui.View):
    """Paginated /check panel with optional per-case removal controls."""

    PER_PAGE = 5

    def __init__(
        self,
        *,
        cog: "ModerationCog",
        owner: discord.Member,
        guild: discord.Guild,
        target: discord.Member,
        can_edit: bool,
    ):
        super().__init__(timeout=600)
        self.cog = cog
        self.bot = cog.bot
        self.owner_id = owner.id
        self.guild = guild
        self.target = target
        self.can_edit = can_edit
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
        embed.add_field(
            name="Total timeout issued",
            value=format_duration(summary["total_timeout_seconds"]),
            inline=True,
        )

        now = discord.utils.utcnow()
        until = self.target.timed_out_until
        if until is not None and until > now:
            embed.add_field(
                name="Currently timed out",
                value=f"Yes — until <t:{int(until.timestamp())}:F>",
                inline=False,
            )
        else:
            embed.add_field(name="Currently timed out", value="No", inline=False)

        if not current:
            embed.add_field(
                name="History",
                value="No warnings or timeout cases are currently recorded in `/check`.",
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
        if self.can_edit:
            for row in current:
                self.add_item(HistoryCaseButton(row))
        if total_pages > 1:
            self.add_item(
                HistoryPageButton(direction=-1, disabled=self.page <= 0)
            )
            self.add_item(
                HistoryPageButton(direction=1, disabled=self.page >= total_pages - 1)
            )

        footer = f"Page {self.page + 1}/{total_pages} • User ID: {self.target.id}"
        if self.can_edit and current:
            footer += " • Red ❌ removes that case from /check history"
        embed.set_footer(text=footer)
        return embed

    async def remove_case(
        self, interaction: discord.Interaction, kind: str, case_id: int
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the staff member who opened this panel can use these controls.",
                ephemeral=True,
            )
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This control only works in a server.", ephemeral=True)
            return

        perms = await self.bot.moderation_permissions.for_member(interaction.user)
        if not perms.can_manage_settings:
            await interaction.response.send_message(
                "You do not have permission to remove moderation history entries.",
                ephemeral=True,
            )
            return
        target_error = self.cog._basic_target_error(interaction.user, self.target)
        if target_error:
            await interaction.response.send_message(target_error, ephemeral=True)
            return

        removed = await self.bot.moderation_database.exclude_history_entry(
            self.guild.id, self.target.id, kind, case_id
        )
        if removed is None:
            await interaction.response.send_message(
                "That case is already gone from this user's `/check` history.",
                ephemeral=True,
            )
            return

        embed = await self.render()
        await interaction.response.edit_message(embed=embed, view=self)

        case = ("W" if kind == "warning" else "T") + f"-{case_id:04d}"
        audit = discord.Embed(
            title="Moderation history entry removed",
            description=str(removed.get("reason") or "No reason recorded"),
        )
        audit.add_field(name="User", value=f"{self.target.mention} (`{self.target.id}`)", inline=False)
        audit.add_field(name="Removed case", value=case, inline=True)
        audit.add_field(name="Removed by", value=interaction.user.mention, inline=True)
        audit.add_field(
            name="Note",
            value="The database row was retained for audit, but this case no longer counts in `/check` or `/warnings`.",
            inline=False,
        )
        await self.cog._log(self.guild, audit)


class ClearHistoryConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        cog: "ModerationCog",
        owner: discord.Member,
        guild: discord.Guild,
        target: discord.Member,
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
                f"**{result['warnings']} warning(s)** and **{result['timeouts']} timeout(s)** removed from history.\n"
                "The underlying rows are retained internally for audit. An active Discord timeout, if any, is not lifted."
            ),
            embed=None,
            view=None,
        )

        audit = discord.Embed(title="Moderation history cleared")
        audit.add_field(name="User", value=f"{self.target.mention} (`{self.target.id}`)", inline=False)
        audit.add_field(name="Warnings removed", value=str(result["warnings"]), inline=True)
        audit.add_field(name="Timeouts removed", value=str(result["timeouts"]), inline=True)
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
