"""
Platform-specific extraction tests — fixture-based, no network.

Run with: pytest tests/test_extraction_platforms.py -v
"""
import json

from app.schemas.schemas import Platform
from app.services.extraction_service import (
    _fallback_title,
    _is_instagram_login_wall,
    _parse_reddit_json,
    _parse_tiktok_oembed,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

REDDIT_JSON = json.dumps([
    {"data": {"children": [
        {"data": {
            "title": "Show HN: I built a second brain",
            "author": "snoo42",
            "thumbnail": "https://preview.redd.it/abc.jpg",
            "preview": {"images": [{"source": {"url": "https://preview.redd.it/full.jpg"}}]},
            "id": "abc123",
        }}
    ]}}
])

TIKTOK_OEMBED = json.dumps({
    "title": "my new video #fyp",
    "thumbnail_url": "https://p16-sign.tiktokcdn.com/x.jpg",
    "author_name": "Creator Name",
    "author_unique_id": "creatorhandle",
})

IG_OG_HTML = """
<html><head>
<meta property="og:title" content="NatGeo on Instagram: 'Sunset over the Serengeti'">
<meta property="og:image" content="https://scontent.cdninstagram.com/img.jpg">
</head><body></body></html>
"""

IG_LOGINWALL_HTML = """
<html><head><title>Login • Instagram</title>
<meta property="og:title" content="Instagram">
</head><body>Login and sign up to see photos and videos from your friends.</body></html>
"""


# ── Reddit ────────────────────────────────────────────────────────────────────

class TestReddit:
    def test_parses_title_thumbnail_author(self):
        out = _parse_reddit_json(REDDIT_JSON)
        assert out["title"] == "Show HN: I built a second brain"
        assert out["thumbnail_url"] == "https://preview.redd.it/full.jpg"
        assert out["author_handle"] == "snoo42"

    def test_prefers_preview_over_thumbnail(self):
        out = _parse_reddit_json(REDDIT_JSON)
        assert out["thumbnail_url"].endswith("full.jpg")

    def test_malformed_json_degrades(self):
        out = _parse_reddit_json("not json{")
        assert out["title"] is None
        assert out["thumbnail_url"] is None


# ── TikTok ────────────────────────────────────────────────────────────────────

class TestTikTok:
    def test_parses_oembed_fields(self):
        out = _parse_tiktok_oembed(TIKTOK_OEMBED)
        assert out["title"] == "my new video #fyp"
        assert out["thumbnail_url"] == "https://p16-sign.tiktokcdn.com/x.jpg"
        assert out["author"] == "Creator Name"
        assert out["author_handle"] == "creatorhandle"

    def test_malformed_json_degrades(self):
        out = _parse_tiktok_oembed("{{{")
        assert out["title"] is None


# ── Instagram ─────────────────────────────────────────────────────────────────

class TestInstagram:
    def test_og_html_accepted_when_not_login_wall(self):
        out = _is_instagram_login_wall(IG_OG_HTML)
        assert out is False

    def test_login_wall_detected(self):
        assert _is_instagram_login_wall(IG_LOGINWALL_HTML) is True

    def test_login_wall_on_empty(self):
        assert _is_instagram_login_wall("") is True


# ── Fallback title derivation ─────────────────────────────────────────────────

class TestFallbackTitle:
    def test_handle_title(self):
        assert _fallback_title("https://www.instagram.com/natgeo/p/C123/", Platform.INSTAGRAM) == "@natgeo — Instagram"

    def test_tiktok_handle(self):
        assert _fallback_title("https://www.tiktok.com/@creator/video/123", Platform.TIKTOK) == "@creator — TikTok"

    def test_reddit_subreddit(self):
        assert _fallback_title("https://www.reddit.com/r/python/comments/abc/slug/", Platform.REDDIT) == "@python — Reddit"

    def test_slug_cleanup(self):
        assert _fallback_title("https://example.com/my-great_article?x=1", Platform.WEB) == "my great article"

    def test_slug_truncated_80(self):
        url = "https://example.com/" + "word-dash-" * 30
        out = _fallback_title(url, Platform.WEB)
        assert len(out) <= 80

    def test_nothing_derivable(self):
        assert _fallback_title("https://example.com", Platform.WEB) == "Web post"
