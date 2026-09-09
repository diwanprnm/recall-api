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

import json
import re
from typing import TypedDict
from urllib.parse import urlparse

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

    Platform-specific extractors (Reddit JSON, TikTok oEmbed, Instagram og)
    run first and win when they return usable metadata; the generic og/HTML
    path covers everything else. Final safety net: derive a readable title
    from the URL so stored items never carry login-wall text or raw slugs.

    Falls back gracefully: if extraction fails, we return what's available.
    """
    logger.info("Extracting content", url=url)

    platform = Platform.from_url(url)

    # Platform-specific structured sources first (each swallows its own errors)
    if platform in (Platform.REDDIT, Platform.TIKTOK):
        structured = await _extract_structured(url, platform)
        if structured is not None:
            # Fill remaining fields from the generic HTML pass for text etc.
            try:
                generic = await _extract_generic(url)
            except ContentExtractionError:
                generic = _fallback_result(url)
            merged = dict(generic)
            merged.update({k: v for k, v in structured.items() if v})
            return merged  # type: ignore[return-value]

    try:
        result = await _extract_generic(url)
    except ContentExtractionError:
        logger.warning("Content extraction failed, returning fallback", url=url)
        result = _fallback_result(url)
    else:
        # Instagram: discard login-wall garbage before it reaches the DB
        html = result.get("_raw_html")
        if platform == Platform.INSTAGRAM and html is not None and _is_instagram_login_wall(html):
            result = _strip_instagram_login_wall(url, result)

    # Guarantee a human-readable title for anything we could parse
    if not result.get("title"):
        result["title"] = _fallback_title(url, platform)
    return result


async def _extract_structured(url: str, platform: Platform) -> dict | None:
    """Fetch structured metadata for platforms with public JSON endpoints."""
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            if platform == Platform.REDDIT:
                json_url = url.rstrip("/") + ".json"
                resp = await client.get(json_url, headers={"User-Agent": "RecallBot/1.0"})
                resp.raise_for_status()
                parsed = _parse_reddit_json(resp.text)
            else:  # TIKTOK
                resp = await client.get(
                    "https://www.tiktok.com/oembed",
                    params={"url": url},
                    headers={"User-Agent": "RecallBot/1.0"},
                )
                resp.raise_for_status()
                parsed = _parse_tiktok_oembed(resp.text)
            return {k: v for k, v in parsed.items() if v} or None
    except Exception as exc:
        logger.warning("Structured extraction failed", platform=platform.value, error=str(exc), url=url)
        return None


def _parse_reddit_json(raw: str) -> ExtractedContent:
    empty: ExtractedContent = ExtractedContent(
        title=None, text=None, author=None, author_handle=None, author_avatar=None,
        thumbnail_url=None, platform=Platform.REDDIT, original_id=None,
        description=None, language=None, published_at=None,
    )
    try:
        data = json.loads(raw)
        post = data[0]["data"]["children"][0]["data"]
    except Exception:
        return empty
    preview = None
    images = post.get("preview", {}).get("images") or []
    if images:
        preview = images[0].get("source", {}).get("url")
    return ExtractedContent(
        title=post.get("title"),
        text=None,
        author=None,
        author_handle=post.get("author"),
        author_avatar=None,
        thumbnail_url=preview or post.get("thumbnail") or None,
        platform=Platform.REDDIT,
        original_id=post.get("id"),
        description=None,
        language=None,
        published_at=None,
    )


def _parse_tiktok_oembed(raw: str) -> ExtractedContent:
    """Parse a TikTok oEmbed response into extraction fields."""
    empty: ExtractedContent = ExtractedContent(
        title=None, text=None, author=None, author_handle=None, author_avatar=None,
        thumbnail_url=None, platform=Platform.TIKTOK, original_id=None,
        description=None, language=None, published_at=None,
    )
    try:
        data = json.loads(raw)
    except Exception:
        return empty
    return ExtractedContent(
        title=data.get("title"),
        text=None,
        author=data.get("author_name"),
        author_handle=data.get("author_unique_id"),
        author_avatar=None,
        thumbnail_url=data.get("thumbnail_url"),
        platform=Platform.TIKTOK,
        original_id=None,
        description=None,
        language=None,
        published_at=None,
    )


_LOGIN_WALL_MARKERS = ("log in", "login", "sign up to see", "see posts and videos")


def _is_instagram_login_wall(html: str) -> bool:
    """Detect Instagram's logged-out login-wall page."""
    if not html:
        return True
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").lower() if soup.title and soup.title.string else ""
    body = soup.get_text(separator=" ", strip=True).lower()[:2000]
    if "instagram" in title and any(m in title for m in ("login", "log in")):
        return True
    return "login and sign up" in body or ("log in" in body and "sign up" in body)


def _strip_instagram_login_wall(url: str, result: ExtractedContent) -> ExtractedContent:
    """Drop og-derived garbage when Instagram served its login-wall."""
    handle = _handle_from_url(url)
    if handle and (not result.get("title") or result.get("title", "").lower() == "instagram"):
        result["title"] = None  # force fallback derivation
    if handle and not result.get("author_handle"):
        result["author_handle"] = handle
    return result


def _handle_from_url(url: str) -> str | None:
    """Best-effort creator/subreddit handle from a platform URL path."""
    path = urlparse(url).path.strip("/")
    if not path:
        return None
    parts = path.split("/")
    # instagram.com/<user>/p/... · tiktok.com/@user/video/... · reddit.com/r/<sub>/...
    if parts[0].startswith("@"):
        return parts[0].lstrip("@")
    if parts[0] == "r" and len(parts) > 1:
        return parts[1]
    if parts[0] in ("p", "reel", "tv") and len(parts) < 2:
        return None
    return parts[0] if parts[0] not in ("p", "reel", "tv", "comments") else None


_PLATFORM_LABELS = {
    Platform.TWITTER: "Twitter/X",
    Platform.INSTAGRAM: "Instagram",
    Platform.YOUTUBE: "YouTube",
    Platform.REDDIT: "Reddit",
    Platform.LINKEDIN: "LinkedIn",
    Platform.TIKTOK: "TikTok",
    Platform.FACEBOOK: "Facebook",
    Platform.WEB: "Web",
    Platform.OTHER: "Other",
}


def _fallback_title(url: str, platform: Platform) -> str:
    """Derive a readable title when extraction yielded none.

    `@handle — Platform` when a handle is derivable, else the last URL
    segment cleaned up, else the platform label. Never returns a raw slug.
    """
    label = _PLATFORM_LABELS.get(platform, "Web")
    handle = _handle_from_url(url)
    if handle and platform != Platform.WEB:
        return f"@{handle} — {label}"[:80]
    path = urlparse(url).path.strip("/")
    if path:
        segment = path.split("/")[-1]
        segment = re.sub(r"\.(html?|php)$", "", segment)
        cleaned = re.sub(r"[-_]+", " ", segment).strip()
        if cleaned:
            return cleaned[:80]
    return f"{label} post"


async def _extract_generic(url: str) -> ExtractedContent:
    """Generic og/HTML extraction path (previous extract_content body)."""
    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
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

    if "text/html" not in response.headers.get("content-type", "") and "application/xhtml" not in response.headers.get("content-type", ""):
        logger.info("Non-HTML content type, skipping parse", content_type=response.headers.get("content-type"))
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

    return {
        "title": _clean_text(title) if title else None,
        "text": _clean_text(text) if text else None,
        "author": _clean_text(author),
        "author_handle": author_handle,
        "author_avatar": og.author_image,
        "thumbnail_url": thumbnail_url,
        "platform": platform,
        "original_id": original_id,
        "description": _clean_text(description),
        "language": og.locale or meta.language,
        "published_at": og.published_time,
        "_raw_html": html,
    }


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
