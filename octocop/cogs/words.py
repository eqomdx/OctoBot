from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..default_banned_words import DEFAULT_BANNED_WORDS
from ..wordfilter import WordFilter, normalize_word

if TYPE_CHECKING:
    from octobot.bot import OctoBot

log = logging.getLogger(__name__)

FILTER_TIMEOUT_SECONDS = 30


class WordFilterCog(
    commands.GroupCog,
    group_name="word",
    group_description="Manage the banned-word filter.",
):
    """Delete messages containing a banned word and time the author out for 30 seconds.

    Replaces the Arcane keyword filter. The list lives in the shared database and is
    cached in memory so every message is checked without a query.
    """

    def __init__(self, bot: "OctoBot"):
        self.bot = bot
        self.filter = WordFilter()
        self._loaded = False

    async def cog_load(self) -> None:
        await self._ensure_loaded()

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        guild_id = self.bot.config.guild_id
        words = await self.bot.moderation_database.list_banned_words(guild_id)
        if not words:
            # First run: carry the Arcane list over. Only ever done on an empty list so
            # words removed with /word remove stay removed across restarts.
            for word in DEFAULT_BANNED_WORDS:
                await self.bot.moderation_database.add_banned_word(guild_id, normalize_word(word), 0)
            words = await self.bot.moderation_database.list_banned_words(guild_id)
            log.info("Seeded %s default banned words", len(words))
        self.filter = WordFilter(words)
        self._loaded = True

    # ------------------------------------------------------------------ enforcement

    async def _is_exempt(self, member: discord.Member) -> bool:
        if member.bot or member.id == member.guild.owner_id or member.guild_permissions.administrator:
            return True
        perms = await self.bot.moderation_permissions.for_member(member)
        # Anyone holding a staff profile is trusted; the filter targets regular members.
        return any((perms.can_timeout, perms.can_warn, perms.can_check, perms.can_manage_settings))

    async def _enforce(self, message: discord.Message) -> None:
        if message.guild is None or message.guild.id != self.bot.config.guild_id:
            return
        if not isinstance(message.author, discord.Member) or message.author.bot:
            return
        await self._ensure_loaded()
        word = self.filter.find(message.content or "")
        if word is None:
            return
        if await self._is_exempt(message.author):
            return

        guild = message.guild
        member = message.author
        reason = f"OctoBot word filter | banned word: {word}"[:512]
        deleted = True
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException):
            deleted = False
            log.exception("Word filter could not delete message %s", message.id)

        timed_out = False
        now = discord.utils.utcnow()
        current_until = member.timed_out_until
        already_timed_out = current_until is not None and current_until > now
        if not already_timed_out:
            try:
                await member.timeout(timedelta(seconds=FILTER_TIMEOUT_SECONDS), reason=reason)
                timed_out = True
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Word filter could not time out %s", member.id)

        timeout_id = None
        if timed_out:
            bot_id = self.bot.user.id if self.bot.user else 0
            expires_at = now + timedelta(seconds=FILTER_TIMEOUT_SECONDS)
            timeout_id = await self.bot.moderation_database.add_timeout(
                guild.id, member.id, bot_id, FILTER_TIMEOUT_SECONDS,
                f"Automatic: used banned word “{word}”", now, expires_at,
            )
            moderation = self.bot.get_cog("ModerationCog")
            if moderation is not None:
                await moderation._dm_timeout(
                    guild=guild, user=member, reason=f"Your message used a banned word: “{word}”",
                    duration_seconds=FILTER_TIMEOUT_SECONDS, timeout_id=timeout_id,
                )

        # Mod-log: what was said, where, and what happened.
        embed = discord.Embed(title="Banned word filtered", description=(message.content or "")[:2000])
        embed.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="Word", value=f"`{word}`", inline=True)
        embed.add_field(name="Channel", value=f"<#{message.channel.id}>", inline=True)
        outcome = []
        outcome.append("message deleted" if deleted else "**message not deleted** (missing Manage Messages?)")
        if timed_out:
            outcome.append(f"{FILTER_TIMEOUT_SECONDS}s timeout (case `T-{timeout_id:04d}`)")
        elif already_timed_out:
            outcome.append("already timed out")
        else:
            outcome.append("**timeout failed** (missing Moderate Members or role too low?)")
        embed.add_field(name="Action", value=", ".join(outcome), inline=False)
        moderation = self.bot.get_cog("ModerationCog")
        if moderation is not None:
            await moderation._log(guild, embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await self._enforce(message)
        except Exception:
            log.exception("Word filter failed on message %s", getattr(message, "id", None))

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.content == after.content:
            return
        try:
            await self._enforce(after)
        except Exception:
            log.exception("Word filter failed on edited message %s", getattr(after, "id", None))

    # ------------------------------------------------------------------ commands

    async def _require_manage(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Member, discord.Guild] | None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return None
        perms = await self.bot.moderation_permissions.for_member(interaction.user)
        if not perms.can_manage_settings:
            await interaction.response.send_message(
                "You do not have permission to manage the banned-word list.", ephemeral=True
            )
            return None
        return interaction.user, interaction.guild

    @app_commands.command(name="list", description="Show the banned words.")
    @app_commands.guild_only()
    async def list_words(self, interaction: discord.Interaction) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        await self._ensure_loaded()
        words = self.filter.words
        if not words:
            await interaction.response.send_message("The banned-word list is empty.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"Banned words ({len(words)})",
            description="\n".join(f"• `{word}`" for word in words)[:4096],
        )
        embed.set_footer(text=f"Matching is whole-word and case-insensitive • {FILTER_TIMEOUT_SECONDS}s timeout on use")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="add", description="Add a word or phrase to the banned list.")
    @app_commands.describe(word="Word or phrase to ban")
    @app_commands.guild_only()
    async def add_word(self, interaction: discord.Interaction, word: str) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        actor, guild = context
        cleaned = normalize_word(word)
        if not cleaned:
            await interaction.response.send_message("Enter a word to ban.", ephemeral=True)
            return
        if len(cleaned) > 100:
            await interaction.response.send_message("Banned words must be 100 characters or fewer.", ephemeral=True)
            return
        await self._ensure_loaded()
        if not self.filter.add(cleaned):
            await interaction.response.send_message(f"`{cleaned}` is already on the banned list.", ephemeral=True)
            return
        await self.bot.moderation_database.add_banned_word(guild.id, cleaned, actor.id)
        await interaction.response.send_message(
            f"Added `{cleaned}` to the banned-word list ({len(self.filter.words)} total).", ephemeral=True
        )
        audit = discord.Embed(title="Banned word added", description=f"`{cleaned}`")
        audit.add_field(name="By", value=actor.mention, inline=True)
        await self._log(guild, audit)

    @app_commands.command(name="remove", description="Remove a word or phrase from the banned list.")
    @app_commands.describe(word="Word or phrase to unban")
    @app_commands.guild_only()
    async def remove_word(self, interaction: discord.Interaction, word: str) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        actor, guild = context
        cleaned = normalize_word(word)
        await self._ensure_loaded()
        if not self.filter.remove(cleaned):
            await interaction.response.send_message(f"`{cleaned}` is not on the banned list.", ephemeral=True)
            return
        await self.bot.moderation_database.remove_banned_word(guild.id, cleaned)
        await interaction.response.send_message(
            f"Removed `{cleaned}` from the banned-word list ({len(self.filter.words)} remaining).", ephemeral=True
        )
        audit = discord.Embed(title="Banned word removed", description=f"`{cleaned}`")
        audit.add_field(name="By", value=actor.mention, inline=True)
        await self._log(guild, audit)

    @remove_word.autocomplete("word")
    async def remove_word_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        await self._ensure_loaded()
        needle = normalize_word(current)
        matches = [word for word in self.filter.words if needle in word]
        return [app_commands.Choice(name=word, value=word) for word in matches[:25]]

    async def _log(self, guild: discord.Guild, embed: discord.Embed) -> None:
        moderation = self.bot.get_cog("ModerationCog")
        if moderation is not None:
            await moderation._log(guild, embed)
