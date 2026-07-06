"""
Content Extraction Service — extracts rich content from URLs.

Supports:
  1. Direct metadata extraction (Open Graph, Twitter Cards, HTML meta)
  2. Platform-specific extraction via API (Twitter/X via 9router compatible endpoint)
  3. Plain text fallback (readability-lite)

The goal is to go beyond just saving a URL — we want:
  - Full text content (not just title + description)
  - Author name + handle
  - Media thumbnails
  - Thread context (for Twitter threads)
"""
from __future__ import annotations

import re
from typing import TypedDict

import httpx
import structlog
from bs4 import BeautifulSoup

from app.schemas.schemas import Platform

logger = structlog.get_logger(__name__)

# ── Result type ───────────────────────────────────────────────────────────────

class ExtractedContent(TypedDict):
    """Result of content extraction."""
    title: str | None
    text: str | None
    author: str | None
    author_handle: str | None
    author_avatar: str | None
    thumbnail_url: str | None
    platform: Platform
    original_id: str | None
    description: str | None        # meta description
    language: str | None
    published_at: str | None       # ISO 8601 if available


# ── Platform-specific extractors ──────────────────────────────────────────────

_TWITTER_ID_RE = re.compile(r"/status/(\d+)")
_REDDIT_ID_RE = re.compile(r"/comments/(\w+)")
_YOUTUBE_ID_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})"
)


async def extract_content(url: str) -> ExtractedContent:
    """
    Extract rich content from any URL using HTTP GET + HTML parsing.

    For Twitter/X, Reddit, YouTube — we attempt to extract the platform ID
    and return it so the caller can decide whether to use API enrichment.

    Falls back gracefully: if extraction fails, we return what's available.
    """
    logger.info("Extracting content", url=url)

    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; RecallBot/1.0; "
                "+https://recall.app)"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        },
    ) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("HTTP error during extraction", status=exc.response.status_code, url=url)
            raise ContentExtractionError(f"HTTP {exc.response.status_code} for {url}") from exc
        except httpx.RequestError as exc:
            logger.warning("Network error during extraction", error=str(exc), url=url)
            raise ContentExtractionError(f"Failed to reach {url}: {exc}") from exc

    html = response.text
    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        logger.info("Non-HTML content type, skipping parse", content_type=content_type)
        return _fallback_result(url)

    soup = BeautifulSoup(html, "html.parser")

    # Detect platform
    platform = Platform.from_url(url)

    # Extract IDs for API lookup
    original_id = _extract_platform_id(url, platform)

    # Open Graph + Twitter Card metadata
    og = _OpenGraphExtractor(soup)
    meta = _MetaExtractor(soup)

    title = (
        og.title
        or og.twitter_title
        or (soup.title.string if soup.title else None)
        or meta.description
    )
    description = og.description or og.twitter_description or meta.description
    thumbnail_url = og.image or og.twitter_image
    author = og.author or meta.author
    author_handle = None  # hard to get from HTML alone

    # Platform-specific: prefer text content from structured sources
    text = await _extract_text_content(soup, platform, url)

    return ExtractedContent(
        title=_clean_text(title) if title else None,
        text=_clean_text(text) if text else None,
        author=_clean_text(author),
        author_handle=author_handle,
        author_avatar=og.author_image,
        thumbnail_url=thumbnail_url,
        platform=platform,
        original_id=original_id,
        description=_clean_text(description),
        language=og.locale or meta.language,
        published_at=og.published_time,
    )


def _extract_platform_id(url: str, platform: Platform) -> str | None:
    """Extract the platform-specific post ID from URL."""
    if platform == Platform.TWITTER:
        m = _TWITTER_ID_RE.search(url)
        return m.group(1) if m else None
    if platform == Platform.REDDIT:
        m = _REDDIT_ID_RE.search(url)
        return m.group(1) if m else None
    if platform == Platform.YOUTUBE:
        m = _YOUTUBE_ID_RE.search(url)
        return m.group(1) if m else None
    return None


async def _extract_text_content(
    soup: BeautifulSoup,
    platform: Platform,
    url: str,
) -> str | None:
    """
    Extract meaningful text content from the HTML page.

    Strategy:
    1. Try ld+json structured data first (articles have this)
    2. Try article/main content blocks
    3. Fall back to all paragraph text
    4. Strip navigation, ads, footers
    """
    # Priority: ld+json article body
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            import json
            script_content = script.string
            if not script_content:
                continue
            data = json.loads(script_content)
            if isinstance(data, dict):
                if data.get("@type") in {"Article", "NewsArticle", "BlogPosting"}:
                    text = data.get("articleBody") or data.get("text")
                    if text:
                        return text[:50_000]
                elif data.get("@type") == "ItemList":
                    # Twitter moments, Reddit threads
                    items = data.get("itemListElement", [])
                    parts = []
                    for item in items[:50]:
                        if isinstance(item, dict) and (text := item.get("text")):
                            parts.append(text)
                    if parts:
                        return "\n\n".join(parts)
        except Exception:
            continue

    # Try <article> or <main>
    for tag in ["article", "main"]:
        elem = soup.find(tag)
        if elem:
            text = elem.get_text(separator="\n", strip=True)
            if len(text) > 100:
                return text[:50_000]

    # Fallback: all <p> tags
    paragraphs = [p.get_text(strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 50]
    if paragraphs:
        return "\n\n".join(paragraphs)[:50_000]

    return None


# ── HTML Parsing helpers ──────────────────────────────────────────────────────

class _OpenGraphExtractor:
    def __init__(self, soup: BeautifulSoup) -> None:
        self.soup = soup
        self._og = {m["property"]: m["content"] for m in soup.find_all("meta", property=True)}
        self._twitter = {m["name"]: m["content"] for m in soup.find_all("meta", attrs={"name": True}) if m.get("name", "").startswith("twitter:")}

    @property
    def title(self) -> str | None:
        return self._og.get("og:title")

    @property
    def description(self) -> str | None:
        return self._og.get("og:description")

    @property
    def image(self) -> str | None:
        return self._og.get("og:image")

    @property
    def author(self) -> str | None:
        return self._og.get("article:author") or self._og.get("og:article:author")

    @property
    def author_image(self) -> str | None:
        return self._og.get("og:article:author:image")

    @property
    def published_time(self) -> str | None:
        return self._og.get("article:published_time")

    @property
    def locale(self) -> str | None:
        return self._og.get("og:locale")

    @property
    def twitter_title(self) -> str | None:
        return self._twitter.get("twitter:title")

    @property
    def twitter_description(self) -> str | None:
        return self._twitter.get("twitter:description")

    @property
    def twitter_image(self) -> str | None:
        return self._twitter.get("twitter:image")


class _MetaExtractor:
    def __init__(self, soup: BeautifulSoup) -> None:
        self.soup = soup
        self._meta = {m.get("name", "").lower(): m.get("content", "") for m in soup.find_all("meta") if m.get("name")}

    @property
    def description(self) -> str | None:
        return self._meta.get("description")

    @property
    def author(self) -> str | None:
        return self._meta.get("author")

    @property
    def language(self) -> str | None:
        return self._meta.get("language") or self.soup.get("lang")


def _clean_text(text: str | None) -> str | None:
    """Normalize whitespace in extracted text."""
    if not text:
        return None
    return re.sub(r"\s+", " ", text).strip()


def _fallback_result(url: str) -> ExtractedContent:
    """Return minimal result when extraction completely fails."""
    return ExtractedContent(
        title=None,
        text=None,
        author=None,
        author_handle=None,
        author_avatar=None,
        thumbnail_url=None,
        platform=Platform.from_url(url),
        original_id=None,
        description=None,
        language=None,
        published_at=None,
    )


class ContentExtractionError(Exception):
    """Raised when content extraction fails."""
    pass
