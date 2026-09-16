from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from octocop.cogs.moderation import ModerationCog
from octocop.history_ui import PagedEmbedView


def _rows(count: int, content: str) -> list[dict]:
    created = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc).isoformat()
    return [
        {
            "message_id": 1000 - index,
            "channel_id": 5,
            "content": content,
            "attachment_count": 0,
            "created_at": created,
            "edited_at": None,
            "deleted_at": None,
        }
        for index in range(count)
    ]


class MessageHistoryPagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cog = ModerationCog(SimpleNamespace())
        self.guild = SimpleNamespace(id=1)
        self.user = SimpleNamespace(id=2, __str__=lambda self: "Someone")

    def test_twenty_short_messages_make_two_pages_of_ten(self) -> None:
        pages = self.cog._message_history_embeds(
            guild=self.guild, user=self.user, messages=_rows(20, "hi")
        )
        self.assertEqual(len(pages), 2)
        self.assertEqual([len(page.fields) for page in pages], [10, 10])
        self.assertIn("Page 1/2", pages[0].footer.text)
        self.assertIn("Page 2/2", pages[1].footer.text)
        # Oldest first, ending with the most recent.
        self.assertTrue(pages[1].fields[-1].name.startswith("Most recent"))

    def test_long_messages_are_split_to_stay_under_the_embed_limit(self) -> None:
        pages = self.cog._message_history_embeds(
            guild=self.guild, user=self.user, messages=_rows(20, "x" * 900)
        )
        self.assertGreater(len(pages), 2)
        self.assertEqual(sum(len(page.fields) for page in pages), 20)
        for page in pages:
            self.assertLessEqual(len(page), 6000)

    def test_view_only_has_buttons_when_there_are_multiple_pages(self) -> None:
        owner = SimpleNamespace(id=9)
        one = self.cog._message_history_embeds(guild=self.guild, user=self.user, messages=_rows(3, "a"))
        self.assertEqual(len(PagedEmbedView(owner=owner, pages=one).children), 0)
        two = self.cog._message_history_embeds(guild=self.guild, user=self.user, messages=_rows(15, "a"))
        view = PagedEmbedView(owner=owner, pages=two)
        previous, next_button = view.children
        self.assertTrue(previous.disabled)
        self.assertFalse(next_button.disabled)
        self.assertIs(view.current, two[0])



class CaseIdParsingTests(unittest.TestCase):
    def test_accepts_the_shown_format_and_lenient_variants(self) -> None:
        from octocop.history_ui import parse_case_id
        self.assertEqual(parse_case_id("W-0003"), ("warning", 3))
        self.assertEqual(parse_case_id("t-0012"), ("timeout", 12))
        self.assertEqual(parse_case_id("B1"), ("ban", 1))
        self.assertEqual(parse_case_id(" n 7 "), ("note", 7))
        self.assertIsNone(parse_case_id("X-0001"))
        self.assertIsNone(parse_case_id("0003"))
        self.assertIsNone(parse_case_id("W-"))
        self.assertIsNone(parse_case_id(""))


if __name__ == "__main__":
    unittest.main()
