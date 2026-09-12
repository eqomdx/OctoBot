from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..duration import DurationError, format_duration, parse_duration
from ..history_ui import ClearHistoryConfirmView, ModerationHistoryView
from ..permissions import moderation_target_error
from ..ui import timeout_embed, untimeout_embed, warning_embed

if TYPE_CHECKING:
    from octobot.bot import OctoBot

log = logging.getLogger(__name__)


class ModerationCog(commands.Cog):
    def __init__(self, bot: "OctoBot"):
        self.bot = bot


    async def _index_message(self, message: discord.Message) -> None:
        if message.guild is None or message.guild.id != self.bot.config.guild_id:
            return
        if message.author.bot:
            return
        await self.bot.moderation_database.record_message(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            user_id=message.author.id,
            message_id=message.id,
            content=message.content or "",
            attachment_count=len(message.attachments),
            created_at=message.created_at,
            edited_at=message.edited_at,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await self._index_message(message)
        except Exception:
            log.exception("Failed to index Discord message %s", getattr(message, "id", None))

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        try:
            await self._index_message(after)
        except Exception:
            log.exception("Failed to update indexed Discord message %s", getattr(after, "id", None))

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id != self.bot.config.guild_id:
            return
        try:
            await self.bot.moderation_database.mark_message_deleted(payload.guild_id, payload.message_id)
        except Exception:
            log.exception("Failed to mark deleted message %s in /history index", payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id != self.bot.config.guild_id:
            return
        try:
            await self.bot.moderation_database.mark_messages_deleted(payload.guild_id, payload.message_ids)
        except Exception:
            log.exception("Failed to mark bulk-deleted messages in /history index")

    async def _member_and_guild(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Member, discord.Guild] | None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return None
        return interaction.user, interaction.guild

    async def _log(self, guild: discord.Guild, embed: discord.Embed) -> None:
        settings = await self.bot.moderation_database.get_guild_settings(guild.id)
        channel_id = settings.get("mod_log_channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("Failed to send moderation log in guild %s", guild.id)

    @staticmethod
    def _basic_target_error(actor: discord.Member, target: discord.Member) -> str | None:
        guild = actor.guild
        if actor.id == target.id:
            return "You cannot moderate yourself."
        if target.id == guild.owner_id:
            return "The server owner cannot be moderated by this bot."
        if target.bot:
            return "This bot is configured to moderate human members only."
        if actor.id != guild.owner_id and not actor.guild_permissions.administrator:
            if actor.top_role <= target.top_role:
                return "You cannot moderate a member with an equal or higher Discord role than your own."
        return None

    async def _dm_warning(
        self,
        *,
        guild: discord.Guild,
        user: discord.Member,
        moderator: discord.Member,
        reason: str,
        warning_id: int,
    ) -> bool:
        settings = await self.bot.moderation_database.get_guild_settings(guild.id)
        if not bool(settings["dm_warnings"]):
            return False
        embed = discord.Embed(
            title=f"Warning from {guild.name}",
            description=reason,
        )
        embed.add_field(name="Moderator", value=str(moderator), inline=True)
        embed.add_field(name="Warning", value=f"W-{warning_id:04d}", inline=True)
        try:
            await user.send(embed=embed)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _dm_timeout(
        self,
        *,
        guild: discord.Guild,
        user: discord.Member,
        moderator: discord.Member,
        reason: str,
        duration_seconds: int,
        timeout_id: int,
    ) -> bool:
        settings = await self.bot.moderation_database.get_guild_settings(guild.id)
        if not bool(settings["dm_timeouts"]):
            return False
        embed = discord.Embed(
            title=f"You were timed out in {guild.name}",
            description=reason,
        )
        embed.add_field(name="Duration", value=format_duration(duration_seconds), inline=True)
        embed.add_field(name="Moderator", value=str(moderator), inline=True)
        embed.add_field(name="Timeout", value=f"T-{timeout_id:04d}", inline=True)
        try:
            await user.send(embed=embed)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _cleanup_recent_messages(
        self,
        *,
        guild: discord.Guild,
        user: discord.Member,
        minutes: int,
        moderator: discord.Member,
    ) -> tuple[int, int, int]:
        """Delete the target user's recent messages in accessible guild message channels.

        Returns (messages_deleted, channels_scanned, channels_failed_or_skipped).
        Cleanup is deliberately best-effort: a permission problem in one channel never
        rolls back an already-successful timeout.
        """
        if minutes <= 0:
            return 0, 0, 0

        bot_member = guild.me
        if bot_member is None:
            return 0, 0, 1

        cutoff = discord.utils.utcnow() - timedelta(minutes=minutes)
        deleted = 0
        scanned = 0
        skipped_or_failed = 0
        seen: set[int] = set()

        # guild.channels covers normal guild channels. guild.threads adds active/cached
        # threads (including forum posts). Only objects exposing history() are scanned.
        candidates = [*guild.channels, *guild.threads]
        for channel in candidates:
            channel_id = getattr(channel, "id", None)
            if channel_id is None or channel_id in seen or not hasattr(channel, "history"):
                continue
            seen.add(channel_id)

            try:
                channel_perms = channel.permissions_for(bot_member)
            except (AttributeError, TypeError):
                continue

            if not (
                channel_perms.view_channel
                and channel_perms.read_message_history
                and channel_perms.manage_messages
            ):
                skipped_or_failed += 1
                continue

            scanned += 1
            try:
                async for message in channel.history(limit=None, after=cutoff, oldest_first=False):
                    if message.author.id != user.id:
                        continue
                    try:
                        await message.delete(
                            reason=(
                                f"OctoBot cleanup | {moderator} ({moderator.id}) | "
                                f"timeout cleanup {minutes}m"
                            )[:512]
                        )
                        deleted += 1
                    except discord.NotFound:
                        # It was already removed between fetching and deleting.
                        continue
                    except (discord.Forbidden, discord.HTTPException):
                        skipped_or_failed += 1
                        log.exception(
                            "Failed to delete message %s during timeout cleanup in channel %s",
                            message.id,
                            channel_id,
                        )
            except (discord.Forbidden, discord.HTTPException):
                skipped_or_failed += 1
                log.exception(
                    "Failed to read channel %s during timeout cleanup in guild %s",
                    channel_id,
                    guild.id,
                )

        return deleted, scanned, skipped_or_failed

    @app_commands.command(name="timeout", description="Time out a user for an exact duration.")
    @app_commands.describe(
        user="User to time out",
        duration="Duration using s, m, h, d, w (examples: 30m, 1h30m, 2d)",
        reason="Reason for the timeout",
    )
    @app_commands.guild_only()
    async def timeout_command(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str,
        reason: str,
    ) -> None:
        context = await self._member_and_guild(interaction)
        if context is None:
            return
        actor, guild = context
        perms = await self.bot.moderation_permissions.for_member(actor)
        if not perms.can_timeout:
            await interaction.response.send_message("You do not have permission to use `/timeout`.", ephemeral=True)
            return

        try:
            seconds = parse_duration(duration)
        except DurationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if seconds > perms.max_timeout_seconds:
            await interaction.response.send_message(
                f"Your maximum timeout is **{format_duration(perms.max_timeout_seconds)}**.",
                ephemeral=True,
            )
            return

        reason = reason.strip()
        if not reason:
            await interaction.response.send_message("A reason is required.", ephemeral=True)
            return
        if len(reason) > 1000:
            await interaction.response.send_message("Reason must be 1000 characters or fewer.", ephemeral=True)
            return

        bot_member = guild.me
        if bot_member is None:
            await interaction.response.send_message("I could not resolve my server member record.", ephemeral=True)
            return
        target_error = moderation_target_error(actor, user, bot_member)
        if target_error:
            await interaction.response.send_message(target_error, ephemeral=True)
            return

        now = discord.utils.utcnow()
        current_until = user.timed_out_until
        if current_until is not None and current_until > now:
            await interaction.response.send_message(
                f"{user.mention} is already timed out until <t:{int(current_until.timestamp())}:F>. Remove it first with `/untimeout`.",
                ephemeral=True,
            )
            return

        expires_at = now + timedelta(seconds=seconds)
        audit_reason = f"OctoBot | {actor} ({actor.id}) | {reason}"[:512]

        # Cleanup can scan several channels, so acknowledge the interaction before
        # performing network operations that may take longer than Discord's response window.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await user.timeout(timedelta(seconds=seconds), reason=audit_reason)
        except discord.Forbidden:
            await interaction.followup.send(
                "Discord refused the timeout. Check that I have **Moderate Members** and that my role is above the target user.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Discord rejected the timeout: `{exc}`", ephemeral=True)
            return

        timeout_id = await self.bot.moderation_database.add_timeout(
            guild.id,
            user.id,
            actor.id,
            seconds,
            reason,
            now,
            expires_at,
        )

        settings = await self.bot.moderation_database.get_guild_settings(guild.id)
        cleanup_minutes = int(settings.get("timeout_cleanup_minutes") or 0)
        deleted_count = 0
        cleanup_scanned = 0
        cleanup_issues = 0
        if cleanup_minutes > 0:
            deleted_count, cleanup_scanned, cleanup_issues = await self._cleanup_recent_messages(
                guild=guild,
                user=user,
                minutes=cleanup_minutes,
                moderator=actor,
            )
            await self.bot.moderation_database.set_timeout_cleanup_result(
                timeout_id, cleanup_minutes, deleted_count
            )

        dm_sent = await self._dm_timeout(
            guild=guild,
            user=user,
            moderator=actor,
            reason=reason,
            duration_seconds=seconds,
            timeout_id=timeout_id,
        )
        embed = timeout_embed(
            user=user,
            moderator_id=actor.id,
            reason=reason,
            timeout_id=timeout_id,
            duration_seconds=seconds,
            expires_at=expires_at,
        )
        if cleanup_minutes > 0:
            embed.add_field(
                name="Message cleanup",
                value=(
                    f"Previous **{cleanup_minutes}m** • **{deleted_count}** message(s) deleted\n"
                    f"Channels scanned: {cleanup_scanned} • skipped/failed: {cleanup_issues}"
                ),
                inline=False,
            )
        await self._log(guild, embed)
        dm_note = " DM sent." if dm_sent else " DM was not sent (disabled or unavailable)."
        cleanup_note = (
            f" Deleted **{deleted_count}** message(s) from the previous **{cleanup_minutes}m**"
            f" ({cleanup_issues} skipped/failed channel operation(s))."
            if cleanup_minutes > 0
            else ""
        )
        await interaction.followup.send(
            f"Timed out {user.mention} for **{format_duration(seconds)}**. Case `T-{timeout_id:04d}`."
            f"{cleanup_note}{dm_note}",
            ephemeral=True,
        )

    @app_commands.command(name="untimeout", description="Remove a user's active timeout early.")
    @app_commands.describe(user="User whose timeout should be removed", reason="Reason for removing it")
    @app_commands.guild_only()
    async def untimeout_command(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str = "Timeout removed by staff",
    ) -> None:
        context = await self._member_and_guild(interaction)
        if context is None:
            return
        actor, guild = context
        perms = await self.bot.moderation_permissions.for_member(actor)
        if not perms.can_untimeout:
            await interaction.response.send_message("You do not have permission to use `/untimeout`.", ephemeral=True)
            return

        bot_member = guild.me
        if bot_member is None:
            await interaction.response.send_message("I could not resolve my server member record.", ephemeral=True)
            return
        target_error = moderation_target_error(actor, user, bot_member)
        if target_error:
            await interaction.response.send_message(target_error, ephemeral=True)
            return

        now = discord.utils.utcnow()
        current_until = user.timed_out_until
        if current_until is None or current_until <= now:
            await interaction.response.send_message(f"{user.mention} is not currently timed out.", ephemeral=True)
            return

        reason = reason.strip() or "Timeout removed by staff"
        audit_reason = f"OctoBot removal | {actor} ({actor.id}) | {reason}"[:512]
        try:
            await user.timeout(None, reason=audit_reason)
        except discord.Forbidden:
            await interaction.response.send_message("Discord refused to remove the timeout.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(f"Discord rejected the request: `{exc}`", ephemeral=True)
            return

        timeout_id = await self.bot.moderation_database.end_latest_timeout(guild.id, user.id, actor.id, reason)
        embed = untimeout_embed(
            user=user,
            moderator_id=actor.id,
            reason=reason,
            timeout_id=timeout_id,
        )
        await self._log(guild, embed)
        case_text = (
            f" Case `T-{timeout_id:04d}` was removed from `/check` history."
            if timeout_id else ""
        )
        await interaction.response.send_message(
            f"Removed {user.mention}'s timeout.{case_text}", ephemeral=True
        )

    @app_commands.command(name="warn", description="Warn a user and send the warning to them by DM.")
    @app_commands.describe(user="User to warn", reason="Reason for the warning")
    @app_commands.guild_only()
    async def warn_command(
        self, interaction: discord.Interaction, user: discord.Member, reason: str
    ) -> None:
        context = await self._member_and_guild(interaction)
        if context is None:
            return
        actor, guild = context
        perms = await self.bot.moderation_permissions.for_member(actor)
        if not perms.can_warn:
            await interaction.response.send_message("You do not have permission to use `/warn`.", ephemeral=True)
            return

        target_error = self._basic_target_error(actor, user)
        if target_error:
            await interaction.response.send_message(target_error, ephemeral=True)
            return
        reason = reason.strip()
        if not reason:
            await interaction.response.send_message("A reason is required.", ephemeral=True)
            return
        if len(reason) > 1000:
            await interaction.response.send_message("Reason must be 1000 characters or fewer.", ephemeral=True)
            return

        # Acknowledge before DB/DM/log network work. Discord interactions must receive
        # an initial response quickly; a slow DM or log send should never surface as
        # "The application did not respond" to the moderator.
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            warning_id = await self.bot.moderation_database.add_warning(
                guild.id, user.id, actor.id, reason
            )
            dm_sent = await self._dm_warning(
                guild=guild,
                user=user,
                moderator=actor,
                reason=reason,
                warning_id=warning_id,
            )
            embed = warning_embed(
                user=user,
                moderator_id=actor.id,
                reason=reason,
                warning_id=warning_id,
            )
            await self._log(guild, embed)
        except Exception:
            log.exception(
                "Failed to process /warn for target %s in guild %s",
                user.id,
                guild.id,
            )
            await interaction.followup.send(
                "OctoBot could not complete the warning. Check the bot logs for the error.",
                ephemeral=True,
            )
            return

        dm_note = (
            "The user was sent a DM."
            if dm_sent
            else "The warning was saved, but the DM was disabled or could not be delivered."
        )
        await interaction.followup.send(
            f"Warning `W-{warning_id:04d}` recorded for {user.mention}. {dm_note}",
            ephemeral=True,
        )


    async def _backfill_recent_messages(
        self, *, guild: discord.Guild, user: discord.Member, amount: int
    ) -> tuple[int, int]:
        """Best-effort recent backfill for messages sent before this build was online.

        Discord does not expose a bot-friendly server-wide author search endpoint, so this
        scans a bounded number of recent messages from channels the bot can read. New
        messages are indexed continuously and do not need this scan.
        Returns (messages_scanned, matching_messages_indexed).
        """
        bot_member = guild.me
        if bot_member is None:
            return 0, 0

        per_channel_limit = max(100, min(500, amount * 25))
        total_scan_limit = 5000
        scanned = 0
        matched = 0
        seen: set[int] = set()
        candidates = [*guild.text_channels, *guild.threads]
        candidates.sort(
            key=lambda channel: int(getattr(channel, "last_message_id", 0) or 0),
            reverse=True,
        )

        for channel in candidates:
            channel_id = getattr(channel, "id", None)
            if channel_id is None or channel_id in seen or not hasattr(channel, "history"):
                continue
            seen.add(channel_id)
            try:
                channel_perms = channel.permissions_for(bot_member)
            except (AttributeError, TypeError):
                continue
            if not (channel_perms.view_channel and channel_perms.read_message_history):
                continue

            try:
                async for message in channel.history(limit=per_channel_limit, oldest_first=False):
                    scanned += 1
                    if message.author.id == user.id and not message.author.bot:
                        await self._index_message(message)
                        matched += 1
                    if scanned >= total_scan_limit:
                        return scanned, matched
            except (discord.Forbidden, discord.HTTPException):
                continue
        return scanned, matched

    def _message_history_embeds(
        self, *, guild: discord.Guild, user: discord.Member, messages: list[dict]
    ) -> list[discord.Embed]:
        if not messages:
            embed = discord.Embed(
                title=f"Message history — {user}",
                description=(
                    "No indexed messages were found for this user. OctoBot indexes new "
                    "messages while it is online and performs a bounded recent-history scan "
                    "when `/history` is used."
                ),
            )
            embed.set_footer(text=f"User ID: {user.id}")
            return [embed]

        per_page = 10
        pages: list[discord.Embed] = []
        total_pages = (len(messages) + per_page - 1) // per_page
        for page_index in range(total_pages):
            chunk = messages[page_index * per_page : (page_index + 1) * per_page]
            embed = discord.Embed(
                title=f"Message history — {user}",
                description=f"Showing **{len(messages)}** most recent indexed message(s).",
            )
            for row in chunk:
                created = discord.utils.parse_time(str(row["created_at"]))
                ts = int(created.timestamp()) if created is not None else 0
                content = str(row.get("content") or "").strip()
                attachment_count = int(row.get("attachment_count") or 0)
                if not content:
                    content = (
                        f"*[Attachment-only message — {attachment_count} attachment(s)]*"
                        if attachment_count
                        else "*[No text content]*"
                    )
                if len(content) > 850:
                    content = content[:847] + "..."
                message_id = int(row["message_id"])
                channel_id = int(row["channel_id"])
                jump_url = f"https://discord.com/channels/{guild.id}/{channel_id}/{message_id}"
                edited_note = " • edited" if row.get("edited_at") else ""
                deleted_note = " • **deleted**" if row.get("deleted_at") else ""
                location = f"<#{channel_id}> • <t:{ts}:R>{edited_note}{deleted_note}"
                if not row.get("deleted_at"):
                    location += f" • [Jump to message]({jump_url})"
                value = f"{content}\n{location}"
                embed.add_field(name=f"Message `{message_id}`", value=value, inline=False)
            embed.set_footer(
                text=f"Page {page_index + 1}/{total_pages} • User ID: {user.id}"
            )
            pages.append(embed)
        return pages

    def _warnings_embeds(
        self, *, guild: discord.Guild, user: discord.Member, warnings: list[dict]
    ) -> list[discord.Embed]:
        if not warnings:
            embed = discord.Embed(title=f"Warnings — {user}", description="No warnings recorded for this user.")
            embed.set_footer(text=f"User ID: {user.id}")
            return [embed]

        per_page = 10
        pages: list[discord.Embed] = []
        total_pages = (len(warnings) + per_page - 1) // per_page
        for page_index in range(total_pages):
            chunk = warnings[page_index * per_page : (page_index + 1) * per_page]
            embed = discord.Embed(
                title=f"Warnings — {user}",
                description=f"**Total warnings:** {len(warnings)}",
            )
            for row in chunk:
                ts = int(discord.utils.parse_time(row["created_at"]).timestamp())
                reason = str(row["reason"])
                if len(reason) > 780:
                    reason = reason[:777] + "..."
                value = f"**Moderator:** <@{row['moderator_id']}>\n**Date:** <t:{ts}:F>\n**Reason:** {reason}"
                embed.add_field(name=f"W-{int(row['id']):04d}", value=value, inline=False)
            embed.set_footer(
                text=f"Page {page_index + 1}/{total_pages} • User ID: {user.id} • Server: {guild.name}"
            )
            pages.append(embed)
        return pages

    @app_commands.command(name="warnings", description="Show all warnings recorded for a user.")
    @app_commands.describe(
        user="User whose warnings should be shown",
        channel="Optional channel to post the warning history in",
    )
    @app_commands.guild_only()
    async def warnings_command(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        channel: discord.TextChannel | None = None,
    ) -> None:
        context = await self._member_and_guild(interaction)
        if context is None:
            return
        actor, guild = context
        perms = await self.bot.moderation_permissions.for_member(actor)
        if not perms.can_view_warnings:
            await interaction.response.send_message("You do not have permission to use `/warnings`.", ephemeral=True)
            return

        warnings = await self.bot.moderation_database.list_warnings(guild.id, user.id)
        embeds = self._warnings_embeds(guild=guild, user=user, warnings=warnings)

        if channel is not None:
            if channel.guild.id != guild.id:
                await interaction.response.send_message("Choose a channel in this server.", ephemeral=True)
                return
            try:
                for embed in embeds:
                    await channel.send(embed=embed)
            except discord.Forbidden:
                await interaction.response.send_message(
                    f"I cannot send messages or embeds in {channel.mention}.", ephemeral=True
                )
                return
            except discord.HTTPException as exc:
                await interaction.response.send_message(f"Could not post the history: `{exc}`", ephemeral=True)
                return
            await interaction.response.send_message(
                f"Posted all {len(warnings)} warning(s) for {user.mention} in {channel.mention}.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(embed=embeds[0], ephemeral=True)
        for embed in embeds[1:]:
            await interaction.followup.send(embed=embed, ephemeral=True)


    @app_commands.command(name="history", description="Show a user's recent server messages.")
    @app_commands.describe(
        user="User whose recent messages should be shown",
        amount="Number of messages to show (default 10, maximum 50)",
    )
    @app_commands.guild_only()
    async def history_command(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 50] = 10,
    ) -> None:
        context = await self._member_and_guild(interaction)
        if context is None:
            return
        actor, guild = context
        perms = await self.bot.moderation_permissions.for_member(actor)
        # /history follows /check access so Helpers remain limited to timeout tools.
        if not perms.can_check:
            await interaction.response.send_message(
                "You do not have permission to use `/history`.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        messages = await self.bot.moderation_database.list_recent_messages(
            guild.id, user.id, amount
        )
        scan_note = ""
        if len(messages) < amount:
            scanned, matched = await self._backfill_recent_messages(
                guild=guild, user=user, amount=amount
            )
            messages = await self.bot.moderation_database.list_recent_messages(
                guild.id, user.id, amount
            )
            if scanned:
                scan_note = (
                    f"\n*Recent backfill scanned {scanned} message(s) and found "
                    f"{matched} from this user.*"
                )

        embeds = self._message_history_embeds(guild=guild, user=user, messages=messages)
        if scan_note:
            description = embeds[0].description or ""
            embeds[0].description = description + scan_note
        await interaction.followup.send(embed=embeds[0], ephemeral=True)
        for embed in embeds[1:]:
            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="check", description="Show a user's complete moderation history.")
    @app_commands.describe(user="User whose moderation history should be checked")
    @app_commands.guild_only()
    async def check_command(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        context = await self._member_and_guild(interaction)
        if context is None:
            return
        actor, guild = context
        perms = await self.bot.moderation_permissions.for_member(actor)
        if not perms.can_check:
            await interaction.response.send_message(
                "You do not have permission to use `/check`.", ephemeral=True
            )
            return

        # Viewing history and editing history are deliberately separate. Moderators/admins
        # seeded by OctoBot have can_manage_settings, while a role granted view-only
        # /check access cannot erase cases.
        can_edit = bool(perms.can_manage_settings) and self._basic_target_error(actor, user) is None
        view = ModerationHistoryView(
            cog=self,
            owner=actor,
            guild=guild,
            target=user,
            can_edit=can_edit,
        )
        embed = await view.render()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="clearcheck",
        description="Clear a user's recorded warning/timeout history from /check.",
    )
    @app_commands.describe(user="User whose /check profile should be cleared")
    @app_commands.guild_only()
    async def clearcheck_command(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        context = await self._member_and_guild(interaction)
        if context is None:
            return
        actor, guild = context
        perms = await self.bot.moderation_permissions.for_member(actor)
        if not perms.can_manage_settings:
            await interaction.response.send_message(
                "You do not have permission to clear moderation history.", ephemeral=True
            )
            return

        target_error = self._basic_target_error(actor, user)
        if target_error:
            await interaction.response.send_message(target_error, ephemeral=True)
            return

        summary = await self.bot.moderation_database.get_moderation_summary(guild.id, user.id)
        if summary["warning_count"] == 0 and summary["timeout_count"] == 0:
            await interaction.response.send_message(
                f"{user.mention} has no `/check` history to clear.", ephemeral=True
            )
            return

        view = ClearHistoryConfirmView(
            cog=self, owner=actor, guild=guild, target=user
        )
        await interaction.response.send_message(
            (
                f"Clear **all** `/check` history for {user.mention}?\n"
                f"This will remove **{summary['warning_count']} warning(s)** and "
                f"**{summary['timeout_count']} timeout(s)** from `/check` and `/warnings`.\n\n"
                "This does **not** lift an active Discord timeout. The underlying database rows "
                "are retained for audit."
            ),
            view=view,
            ephemeral=True,
        )
