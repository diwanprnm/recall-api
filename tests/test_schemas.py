"""
Unit tests for schemas and services — no external dependencies required.
Run with: pytest tests/test_schemas.py -v
"""
from datetime import datetime

import pytest
from pydantic import ValidationError

from app.schemas.schemas import (
    ContentAnalysis,
    ContentClassification,
    ContentSummary,
    ContentType,
    ExtractedEntities,
    Item,
    ItemCreate,
    Platform,
    SearchQuery,
    Sentiment,
)


class TestPlatform:
    def test_from_url_twitter(self):
        assert Platform.from_url("https://x.com/elonmusk/status/123") == Platform.TWITTER
        assert Platform.from_url("https://twitter.com/elonmusk/status/123") == Platform.TWITTER

    def test_from_url_youtube(self):
        assert Platform.from_url("https://youtube.com/watch?v=abc123") == Platform.YOUTUBE
        assert Platform.from_url("https://youtu.be/abc123") == Platform.YOUTUBE

    def test_from_url_reddit(self):
        assert Platform.from_url("https://reddit.com/r/Python/comments/abc") == Platform.REDDIT

    def test_from_url_instagram(self):
        assert Platform.from_url("https://instagram.com/p/abc123") == Platform.INSTAGRAM

    def test_from_url_linkedin(self):
        assert Platform.from_url("https://linkedin.com/posts/abc") == Platform.LINKEDIN

    def test_from_url_unknown(self):
        assert Platform.from_url("https://example.com/article") == Platform.WEB


class TestItemCreate:
    def test_valid_item_create(self):
        item = ItemCreate(
            url="https://x.com/user/status/123",
            platform=Platform.TWITTER,
            title="Test Thread",
            text="This is my test content",
        )
        assert item.platform == Platform.TWITTER
        assert item.override_tags == []

    def test_url_required(self):
        with pytest.raises(ValidationError):
            ItemCreate(platform=Platform.TWITTER)

    def test_text_truncation(self):
        long_text = "x" * 60_000
        item = ItemCreate(
            url="https://example.com",
            platform=Platform.WEB,
            text=long_text,
        )
        assert len(item.text or "") <= 50_050  # 50K + "[truncated]"
        assert "[truncated]" in (item.text or "")

    def test_platform_from_url_inference(self):
        assert Platform.from_url("https://x.com/test/status/1") == Platform.TWITTER


class TestSearchQuery:
    def test_valid_search_query(self):
        q = SearchQuery(query="AI agents for software development")
        assert q.limit == 20
        assert q.tags == []

    def test_search_query_custom_limit(self):
        q = SearchQuery(query="test", limit=50)
        assert q.limit == 50

    def test_search_query_platform_filter(self):
        q = SearchQuery(query="rust programming", platform=Platform.REDDIT)
        assert q.platform == Platform.REDDIT

    def test_search_query_empty_query_rejected(self):
        with pytest.raises(ValidationError):
            SearchQuery(query="")


class TestContentAnalysis:
    def test_content_analysis_valid(self):
        analysis = ContentAnalysis(
            summary=ContentSummary(
                one_liner="A great tutorial on FastAPI",
                key_points=["FastAPI basics", "Dependency injection"],
            ),
            classification=ContentClassification(
                primary_topics=["Python", "Web Development"],
                content_type=ContentType.ARTICLE,
                sentiment=Sentiment.POSITIVE,
                relevance_score=4,
                actionability="high",
            ),
            entities=ExtractedEntities(
                people=["Miguel Grinberg"],
                technologies=["FastAPI", "Python"],
                hashtags=["fastapi", "python"],
            ),
            suggested_tags=["python", "fastapi", "tutorial", "api"],
            quality_score=5,
        )
        assert analysis.summary.one_liner == "A great tutorial on FastAPI"
        assert analysis.classification.actionability == "high"
        assert "fastapi" in analysis.suggested_tags

    def test_actionability_must_be_valid(self):
        with pytest.raises(ValidationError):
            ContentClassification(
                primary_topics=["Tech"],
                content_type=ContentType.POST,
                sentiment=Sentiment.NEUTRAL,
                relevance_score=3,
                actionability="invalid_value",  # must be high|medium|low|none
            )

    def test_quality_score_bounds(self):
        """relevance_score must be 1-5."""
        with pytest.raises(ValidationError):
            ContentClassification(
                primary_topics=["AI"],
                content_type=ContentType.POST,
                sentiment=Sentiment.NEUTRAL,
                relevance_score=6,  # must be 1-5 → raises ValidationError
                actionability="low",
            )


class TestItem:
    def test_item_from_dict(self):
        now = datetime.utcnow().isoformat()
        item = Item(
            id="abc123",
            user_id="user456",
            url="https://x.com/test/status/1",
            platform="twitter",
            title="Test",
            saved_at=now,
        )
        assert item.id == "abc123"
        assert item.tags == []


class TestSentiment:
    def test_all_sentiments(self):
        assert Sentiment.POSITIVE.value == "positive"
        assert Sentiment.NEGATIVE.value == "negative"
        assert Sentiment.NEUTRAL.value == "neutral"
        assert Sentiment.MIXED.value == "mixed"


class TestContentType:
    def test_all_types(self):
        assert ContentType.THREAD.value == "thread"
        assert ContentType.VIDEO.value == "video"
        assert ContentType.ARTICLE.value == "article"
        assert ContentType.POST.value == "post"
