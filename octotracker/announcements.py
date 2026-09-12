from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup, NavigableString, Tag


LOGGER = logging.getLogger(__name__)
FORUM_URL = "https://octowow.st/forum/viewforum.php?f=2"
NEWS_URL = "https://octowow.st/news.json"


@dataclass(frozen=True, slots=True)
class Announcement:
    topic_id: int
    title: str
    url: str
    author: str | None = None
    published_at: str | None = None
    preview: str | None = None
    body: str | None = None
    content_source: str | None = None


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def is_browser_verification(html: str) -> bool:
    lowered = html.casefold()
    return (
        "verifying your browser" in lowered
        or "<title>just a moment please" in lowered
        or "cf-chl-" in lowered
    )


def _feed_body_is_complete(body: str) -> bool:
    stripped = body.rstrip()
    return bool(stripped) and not stripped.endswith(("...", "…"))


def _render_html(node: Tag | NavigableString, base_url: str) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""

    name = node.name.casefold()
    if name in {"script", "style", "blockquote"}:
        return ""
    if name == "br":
        return "\n"

    if name in {"ul", "ol"}:
        items: list[str] = []
        for index, item in enumerate(node.find_all("li", recursive=False), start=1):
            item_text = "".join(
                _render_html(child, base_url) for child in item.children
            ).strip()
            if item_text:
                prefix = f"{index}." if name == "ol" else "-"
                items.append(f"{prefix} {item_text}")
        return "\n".join(items) + "\n\n" if items else ""

    inner = "".join(_render_html(child, base_url) for child in node.children)
    if name in {"strong", "b"} and inner.strip():
        return f"**{inner.strip()}**"
    if name in {"em", "i"} and inner.strip():
        return f"*{inner.strip()}*"
    if name == "code" and inner.strip():
        return f"`{inner.strip()}`"
    if name == "a":
        href = node.get("href")
        label = inner.strip()
        if isinstance(href, str) and label:
            return f"[{label}]({urljoin(base_url, href)})"
        return label
    if name == "li":
        return f"- {inner.strip()}\n"
    if name in {"p", "div", "pre", "h1", "h2", "h3", "h4"}:
        return f"{inner.strip()}\n\n" if inner.strip() else ""
    return inner


def _normalise_rendered_body(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_first_post(html: str, announcement: Announcement) -> Announcement:
    """Extract only phpBB's first post and retain useful Discord formatting."""
    if is_browser_verification(html):
        return announcement

    soup = BeautifulSoup(html, "html.parser")
    first_post: Tag | None = None
    content: Tag | None = None
    for candidate in soup.select(".post"):
        found = candidate.select_one(".postbody .content, .content")
        if isinstance(found, Tag):
            first_post = candidate
            content = found
            break
    if first_post is None or content is None:
        return announcement

    for unwanted in content.select(
        "blockquote, .quote, .signature, .post-buttons, script, style"
    ):
        unwanted.decompose()
    body = _normalise_rendered_body(_render_html(content, announcement.url))

    title = announcement.title
    title_tag = first_post.select_one(".postbody h3 a, h3 a")
    if title_tag:
        parsed_title = _clean_text(title_tag.get_text(" ", strip=True))
        if parsed_title:
            title = parsed_title

    author = announcement.author
    author_tag = first_post.select_one(
        ".postprofile a.username, .postprofile a.username-coloured, "
        ".postprofile .username, .author a.username, .author a.username-coloured"
    )
    if author_tag:
        author = _clean_text(author_tag.get_text(" ", strip=True)) or author

    published_at = announcement.published_at
    time_tag = first_post.select_one(".postbody time[datetime], time[datetime]")
    if time_tag:
        timestamp = time_tag.get("datetime")
        if isinstance(timestamp, str):
            published_at = timestamp.strip() or published_at

    return replace(
        announcement,
        title=title,
        author=author,
        published_at=published_at,
        body=body or announcement.body,
        content_source="forum-first-post" if body else announcement.content_source,
    )


def parse_announcement_listing(html: str) -> list[Announcement]:
    soup = BeautifulSoup(html, "html.parser")
    announcements: list[Announcement] = []
    seen: set[int] = set()

    for anchor in soup.select("a.topictitle[href]"):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        query = parse_qs(urlparse(href).query)
        try:
            topic_id = int(query["t"][0])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if topic_id in seen:
            continue

        container = anchor.find_parent("div", class_="list-inner")
        if container is None:
            container = anchor.find_parent("li")

        author: str | None = None
        published_at: str | None = None
        if isinstance(container, Tag):
            author_tag = container.select_one(
                "a.username, a.username-coloured, span.username"
            )
            if author_tag:
                author = _clean_text(author_tag.get_text(" ", strip=True)) or None
            time_tag = container.select_one("time[datetime]")
            if time_tag:
                timestamp = time_tag.get("datetime")
                if isinstance(timestamp, str):
                    published_at = timestamp.strip() or None

        title = _clean_text(anchor.get_text(" ", strip=True))
        if not title:
            continue
        seen.add(topic_id)
        announcements.append(
            Announcement(
                topic_id=topic_id,
                title=title,
                url=urljoin(FORUM_URL, href),
                author=author,
                published_at=published_at,
            )
        )

    return announcements


def parse_news_feed(payload: Any) -> list[Announcement]:
    """Parse OctoWoW's official forum-backed JSON endpoint."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("News feed does not contain an items list")

    announcements: list[Announcement] = []
    seen: set[int] = set()
    for item in payload["items"]:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        title = item.get("title")
        if not isinstance(url, str) or not isinstance(title, str):
            continue
        query = parse_qs(urlparse(url).query)
        try:
            topic_id = int(query["t"][0])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if topic_id in seen:
            continue

        raw_body = item.get("body")
        body = raw_body.strip() if isinstance(raw_body, str) else None
        if body == "":
            body = None
        complete = bool(body and _feed_body_is_complete(body))
        author = item.get("author")
        published_at = item.get("date")
        seen.add(topic_id)
        announcements.append(
            Announcement(
                topic_id=topic_id,
                title=_clean_text(title),
                url=url,
                author=_clean_text(author) if isinstance(author, str) else None,
                published_at=(
                    published_at.strip()
                    if isinstance(published_at, str)
                    else None
                ),
                preview=body,
                body=body if complete else None,
                content_source="news-json" if complete else "news-preview",
            )
        )
    return announcements


class AnnouncementClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def fetch_topics(self) -> list[Announcement]:
        try:
            async with self.session.get(FORUM_URL) as response:
                response.raise_for_status()
                html = await response.text()
            if is_browser_verification(html):
                LOGGER.warning(
                    "Browser verification blocked the announcement listing; "
                    "trying the official news feed"
                )
                topics = []
            else:
                topics = parse_announcement_listing(html)
            if topics:
                return topics
        except (aiohttp.ClientError, TimeoutError):
            LOGGER.warning("Direct OctoWoW forum request failed; trying news feed")
        except Exception:
            LOGGER.warning("Could not parse the forum page; trying news feed")

        try:
            async with self.session.get(NEWS_URL) as response:
                response.raise_for_status()
                topics = parse_news_feed(await response.json(content_type=None))
            if not topics:
                LOGGER.warning("No announcement topics could be parsed")
            return topics
        except (aiohttp.ClientError, TimeoutError):
            LOGGER.exception("OctoWoW news feed request failed")
        except Exception:
            LOGGER.exception("Could not parse the OctoWoW news feed")
        return []

    async def enrich(self, announcement: Announcement) -> Announcement:
        """Fetch the full first post; retain official JSON as a safe fallback."""
        if announcement.body and announcement.content_source == "news-json":
            return announcement
        try:
            async with self.session.get(announcement.url) as response:
                response.raise_for_status()
                html = await response.text()
            if is_browser_verification(html):
                LOGGER.warning(
                    "Browser verification blocked announcement topic %s; using feed fallback",
                    announcement.topic_id,
                )
                return announcement
            return parse_first_post(html, announcement)
        except (aiohttp.ClientError, TimeoutError):
            LOGGER.warning(
                "Could not fetch first post for announcement topic %s",
                announcement.topic_id,
            )
        except Exception:
            LOGGER.exception(
                "Could not parse announcement topic %s", announcement.topic_id
            )
        return announcement
