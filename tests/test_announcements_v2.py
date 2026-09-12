from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from octotracker.announcements import (
    Announcement,
    AnnouncementClient,
    is_browser_verification,
    parse_first_post,
    parse_news_feed,
)
from octotracker.database import Database
from octotracker.embeds import announcement_embeds, split_announcement_body


class _FakeResponse:
    def __init__(self, html: str, payload=None):
        self.html = html
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self) -> None:
        return None

    async def text(self) -> str:
        return self.html

    async def json(self, content_type=None):
        return self.payload


class _FakeSession:
    def __init__(self, html: str):
        self.html = html

    def get(self, url: str) -> _FakeResponse:
        return _FakeResponse(self.html)


class _FallbackSession:
    def get(self, url: str) -> _FakeResponse:
        if url.endswith("news.json"):
            return _FakeResponse(
                "",
                {
                    "items": [
                        {
                            "title": "Fallback topic",
                            "body": "Fallback body",
                            "url": (
                                "https://octowow.st/forum/"
                                "viewtopic.php?t=123"
                            ),
                        }
                    ]
                },
            )
        return _FakeResponse(
            "<title>Just a moment please</title><p>Verifying your browser</p>"
        )


class AnnouncementParsingTests(unittest.IsolatedAsyncioTestCase):
    def test_first_post_only_with_metadata_and_formatting(self) -> None:
        base = Announcement(
            42, "Listing title", "https://octowow.st/forum/viewtopic.php?t=42"
        )
        html = """
        <div class="post" id="p1">
          <div class="postbody">
            <h3><a href="#p1">Maintenance &amp; launch</a></h3>
            <time datetime="2026-09-10T09:00:00+00:00">Today</time>
            <div class="content">
              <p>Hello <strong>heroes</strong>.</p>
              <p>Read the <a href="/rules">rules</a>.<br>Thank you.</p>
              <ol><li>First step</li><li><em>Second</em> step</li></ol>
              <blockquote>Old quoted content</blockquote>
            </div>
          </div>
          <dl class="postprofile"><a class="username">Kestrel</a></dl>
        </div>
        <div class="post" id="p2">
          <div class="postbody"><div class="content">A reply must not appear.</div></div>
          <dl class="postprofile"><a class="username">Responder</a></dl>
        </div>
        """
        parsed = parse_first_post(html, base)
        self.assertEqual(parsed.title, "Maintenance & launch")
        self.assertEqual(parsed.author, "Kestrel")
        self.assertEqual(parsed.published_at, "2026-09-10T09:00:00+00:00")
        self.assertIn("**heroes**", parsed.body)
        self.assertIn("[rules](https://octowow.st/rules)", parsed.body)
        self.assertIn("1. First step\n2. *Second* step", parsed.body)
        self.assertIn("\n\n", parsed.body)
        self.assertNotIn("quoted", parsed.body)
        self.assertNotIn("reply", parsed.body)
        self.assertEqual(parsed.content_source, "forum-first-post")

    def test_json_full_body_and_truncated_preview(self) -> None:
        full = parse_news_feed(
            {
                "items": [
                    {
                        "title": "Full",
                        "url": "https://octowow.st/forum/viewtopic.php?t=1",
                        "body": "First paragraph\n\nSecond paragraph",
                    }
                ]
            }
        )[0]
        self.assertEqual(full.body, "First paragraph\n\nSecond paragraph")
        self.assertEqual(full.content_source, "news-json")

        preview = parse_news_feed(
            {
                "items": [
                    {
                        "title": "Preview",
                        "url": "https://octowow.st/forum/viewtopic.php?t=2",
                        "body": "Only a preview…",
                    }
                ]
            }
        )[0]
        self.assertIsNone(preview.body)
        self.assertEqual(preview.preview, "Only a preview…")
        self.assertEqual(preview.content_source, "news-preview")

    async def test_browser_verification_gracefully_keeps_preview(self) -> None:
        challenge = "<title>Just a moment please</title><p>Verifying your browser</p>"
        base = Announcement(
            42,
            "Title",
            "https://octowow.st/forum/viewtopic.php?t=42",
            preview="Official preview…",
            content_source="news-preview",
        )
        self.assertTrue(is_browser_verification(challenge))
        result = await AnnouncementClient(_FakeSession(challenge)).enrich(base)
        self.assertEqual(result, base)

    async def test_listing_challenge_uses_official_json_fallback(self) -> None:
        topics = await AnnouncementClient(_FallbackSession()).fetch_topics()
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0].topic_id, 123)
        self.assertEqual(topics[0].body, "Fallback body")

    def test_long_body_splits_at_boundaries_with_page_numbers_and_link(self) -> None:
        body = "\n\n".join(f"Paragraph {index} " + "x" * 700 for index in range(8))
        chunks = split_announcement_body(body, 1000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))
        self.assertEqual("\n\n".join(chunks), body)

        embeds = announcement_embeds(
            Announcement(
                88,
                "Long announcement",
                "https://octowow.st/forum/viewtopic.php?t=88",
                body=body,
            )
        )
        self.assertGreater(len(embeds), 1)
        self.assertIn(f"Page 1/{len(embeds)}", embeds[0].footer.text)
        self.assertIn("Official topic", [field.name for field in embeds[-1].fields])


class AnnouncementDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "announcements.db")
        await self.database.connect()

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temp_dir.cleanup()

    async def test_topic_id_dedup_body_and_delivery_progress_survive_restart(self) -> None:
        item = Announcement(
            77,
            "Title",
            "https://octowow.st/forum/viewtopic.php?t=77",
            body="Full body",
            content_source="forum-first-post",
        )
        await self.database.store_announcement(item)
        await self.database.store_announcement(item)
        self.assertEqual(await self.database.known_topic_ids(), {77})
        await self.database.mark_announcement_page_sent(77, 2)
        await self.database.close()
        self.database = Database(Path(self.temp_dir.name) / "announcements.db")
        await self.database.connect()
        latest = await self.database.latest_announcement()
        self.assertEqual(latest.body, "Full body")
        self.assertEqual(await self.database.announcement_delivery_progress(77), 2)


if __name__ == "__main__":
    unittest.main()
