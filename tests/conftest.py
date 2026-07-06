"""
Test configuration and shared fixtures.
"""
import pytest


@pytest.fixture
def sample_item_data():
    """Minimal valid item creation payload."""
    return {
        "url": "https://x.com/testuser/status/1234567890",
        "platform": "twitter",
        "title": "How to build AI products that matter",
        "text": (
            "A thread on building AI products that solve real problems. "
            "1/ Start with the problem, not the technology. "
            "2/ Talk to users before writing a single line of code. "
            "3/ Measure outcomes, not outputs."
        ),
        "author": "Product Builder",
        "author_handle": "@productbuilder",
    }


@pytest.fixture
def sample_analysis():
    """Mock AI analysis result."""
    from app.schemas.schemas import (
        ContentAnalysis,
        ContentClassification,
        ContentSummary,
        ContentType,
        ExtractedEntities,
        Sentiment,
    )

    return ContentAnalysis(
        summary=ContentSummary(
            one_liner="A practical guide on building AI products by starting with real user problems",
            key_points=[
                "Start with problems, not technology",
                "User research before any implementation",
                "Focus on outcomes over outputs",
            ],
        ),
        classification=ContentClassification(
            primary_topics=["AI", "Product Management", "Startups"],
            content_type=ContentType.THREAD,
            sentiment=Sentiment.POSITIVE,
            relevance_score=5,
            actionability="high",
        ),
        entities=ExtractedEntities(
            people=["Sam Altman"],
            organizations=["OpenAI"],
            technologies=["LLM", "RAG"],
            hashtags=["ai", "product", "startup"],
        ),
        suggested_tags=["ai-products", "product-management", "startup-advice"],
        quality_score=5,
    )