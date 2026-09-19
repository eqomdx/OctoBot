from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..wordfilter import WordFilter, normalize_word

if TYPE_CHECKING:
    from octobot.bot import OctoBot

log = logging.getLogger(__name__)

# The same keyword is not answered again in the same channel for this long, so a
# busy conversation about "down" gets one reply, not one per message.
COOLDOWN_SECONDS = 60
MAX_RESPONSE_LENGTH = 1800


class AutoReplyCog(
    commands.GroupCog,
    group_name="autoreply",
    group_description="Reply automatically when a keyword is said.",
):
    """Fixed responses to keywords, e.g. "down" -> maintenance notice.

    Matching reuses the word-filter matcher: whole-word, case-insensitive, phrases
    allowed. Responses are stored in the shared database and cached in memory.
    """

    def __init__(self, bot: "OctoBot"):
        self.bot = bot
        self.matcher = WordFilter()
        self.responses: dict[str, str] = {}
        self._loaded = False
        self._last_sent: dict[tuple[int, str], float] = {}

    async def cog_load(self) -> None:
        await self._ensure_loaded()

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        rows = await self.bot.moderation_database.list_auto_replies(self.bot.config.guild_id)
        self.responses = {row["keyword"]: row["response"] for row in rows}
        self.matcher = WordFilter(list(self.responses))
        self._loaded = True

    # ------------------------------------------------------------------ replies

    def find(self, text: str) -> tuple[str, str] | None:
        keyword = self.matcher.find(text)
        if keyword is None:
            return None
        response = self.responses.get(keyword)
        return (keyword, response) if response is not None else None

    def _on_cooldown(self, channel_id: int, keyword: str, now: float) -> bool:
        last = self._last_sent.get((channel_id, keyword))
        if last is not None and now - last < COOLDOWN_SECONDS:
            return True
        self._last_sent[(channel_id, keyword)] = now
        return False

    async def handle_message(self, message: discord.Message, now: float | None = None) -> bool:
        """Reply if the message contains a keyword. Returns True when a reply was sent."""
        if message.guild is None or message.guild.id != self.bot.config.guild_id:
            return False
        if message.author.bot:
            return False
        await self._ensure_loaded()
        hit = self.find(message.content or "")
        if hit is None:
            return False
        keyword, response = hit
        if self._on_cooldown(message.channel.id, keyword, now if now is not None else time.monotonic()):
            return False
        try:
            await message.reply(
                response, mention_author=False, allowed_mentions=discord.AllowedMentions.none()
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Auto-reply for %r failed in channel %s", keyword, message.channel.id)
            return False
        return True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await self.handle_message(message)
        except Exception:
            log.exception("Auto-reply failed on message %s", getattr(message, "id", None))

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
                "You do not have permission to manage auto-replies.", ephemeral=True
            )
            return None
        return interaction.user, interaction.guild

    @staticmethod
    def _clean_response(message: str) -> str | None:
        cleaned = message.strip()
        if not cleaned or len(cleaned) > MAX_RESPONSE_LENGTH:
            return None
        return cleaned

    async def _log(self, guild: discord.Guild, title: str, keyword: str, response: str, actor: discord.Member) -> None:
        moderation = self.bot.get_cog("ModerationCog")
        if moderation is None:
            return
        embed = discord.Embed(title=title, description=response[:2000])
        embed.add_field(name="Keyword", value=f"`{keyword}`", inline=True)
        embed.add_field(name="By", value=actor.mention, inline=True)
        await moderation._log(guild, embed)

    @app_commands.command(name="add", description="Reply with a fixed message whenever a keyword is said.")
    @app_commands.describe(keyword="Word or phrase to watch for", message="What the bot replies with")
    @app_commands.guild_only()
    async def add(self, interaction: discord.Interaction, keyword: str, message: str) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        actor, guild = context
        key = normalize_word(keyword)
        if not key or len(key) > 100:
            await interaction.response.send_message("Enter a keyword of 1–100 characters.", ephemeral=True)
            return
        response = self._clean_response(message)
        if response is None:
            await interaction.response.send_message(
                f"Enter a reply of 1–{MAX_RESPONSE_LENGTH} characters.", ephemeral=True
            )
            return
        await self._ensure_loaded()
        if key in self.responses:
            await interaction.response.send_message(
                f"`{key}` already has an auto-reply. Use `/autoreply edit` to change it.", ephemeral=True
            )
            return
        await self.bot.moderation_database.upsert_auto_reply(guild.id, key, response, actor.id)
        self.responses[key] = response
        self.matcher.add(key)
        await interaction.response.send_message(
            f"Added auto-reply for `{key}`:\n> {response}", ephemeral=True
        )
        await self._log(guild, "Auto-reply added", key, response, actor)

    @app_commands.command(name="edit", description="Change the reply for an existing keyword.")
    @app_commands.describe(keyword="Existing keyword", message="New reply")
    @app_commands.guild_only()
    async def edit(self, interaction: discord.Interaction, keyword: str, message: str) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        actor, guild = context
        key = normalize_word(keyword)
        response = self._clean_response(message)
        if response is None:
            await interaction.response.send_message(
                f"Enter a reply of 1–{MAX_RESPONSE_LENGTH} characters.", ephemeral=True
            )
            return
        await self._ensure_loaded()
        if key not in self.responses:
            await interaction.response.send_message(
                f"`{key}` has no auto-reply. Use `/autoreply add` to create one.", ephemeral=True
            )
            return
        await self.bot.moderation_database.upsert_auto_reply(guild.id, key, response, actor.id)
        self.responses[key] = response
        await interaction.response.send_message(
            f"Updated auto-reply for `{key}`:\n> {response}", ephemeral=True
        )
        await self._log(guild, "Auto-reply edited", key, response, actor)

    @app_commands.command(name="remove", description="Stop replying to a keyword.")
    @app_commands.describe(keyword="Keyword to remove")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, keyword: str) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        actor, guild = context
        key = normalize_word(keyword)
        await self._ensure_loaded()
        previous = self.responses.pop(key, None)
        if previous is None:
            await interaction.response.send_message(f"`{key}` has no auto-reply.", ephemeral=True)
            return
        self.matcher.remove(key)
        await self.bot.moderation_database.remove_auto_reply(guild.id, key)
        await interaction.response.send_message(f"Removed the auto-reply for `{key}`.", ephemeral=True)
        await self._log(guild, "Auto-reply removed", key, previous, actor)

    @app_commands.command(name="list", description="Show all auto-replies.")
    @app_commands.guild_only()
    async def list_replies(self, interaction: discord.Interaction) -> None:
        context = await self._require_manage(interaction)
        if context is None:
            return
        await self._ensure_loaded()
        if not self.responses:
            await interaction.response.send_message("No auto-replies are set.", ephemeral=True)
            return
        embed = discord.Embed(title=f"Auto-replies ({len(self.responses)})")
        for key in sorted(self.responses):
            embed.add_field(name=f"`{key}`", value=self.responses[key][:1024], inline=False)
            if len(embed.fields) == 25:
                break
        embed.set_footer(text=f"Whole-word, case-insensitive • one reply per keyword per channel every {COOLDOWN_SECONDS}s")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @edit.autocomplete("keyword")
    @remove.autocomplete("keyword")
    async def keyword_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        await self._ensure_loaded()
        needle = normalize_word(current)
        matches = [key for key in sorted(self.responses) if needle in key]
        return [app_commands.Choice(name=key, value=key) for key in matches[:25]]
