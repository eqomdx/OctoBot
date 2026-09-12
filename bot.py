"""Backward-compatible imports for the original OctoTracker test suite/tools."""

from octobot.bot import HELP_DESCRIPTIONS, MODERATION_HELP_DESCRIPTIONS, OctoBot, main

OctoTracker = OctoBot

__all__ = ["OctoBot", "OctoTracker", "HELP_DESCRIPTIONS", "MODERATION_HELP_DESCRIPTIONS", "main"]
