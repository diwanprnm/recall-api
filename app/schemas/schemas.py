"""
Pydantic schemas — request/response models for all API endpoints.
These also serve as the output schema for the AI pipeline's structured output.

Key design decisions:
  • All IDs are strings (CUIDs from Supabase/Prisma)
  • Datetime fields are ISO 8601 strings (serialised by Pydantic)
  • AI-generated fields are optional (graceful degradation if AI is down)
  • Platform enum is exhaustive — add new platforms here
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


# ── Enums ──────────────────────────────────────────────────────────────────────

class Platform(str, Enum):
    """Supported social media platforms."""
    TWITTER = "twitter"
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"
    REDDIT = "reddit"
    LINKEDIN = "linkedin"
    TIKTOK = "tiktok"
    FACEBOOK = "facebook"
    WEB = "web"           # generic web article
    OTHER = "other"

    @classmethod
    def from_url(cls, url: str) -> Platform:
        """Infer platform from URL — used for quick classification."""
        lower = url.lower()
        if "x.com" in lower or "twitter.com" in lower:
            return cls.TWITTER
        if "instagram.com" in lower:
            return cls.INSTAGRAM
        if "youtube.com" in lower or "youtu.be" in lower:
            return cls.YOUTUBE
        if "reddit.com" in lower:
            return cls.REDDIT
        if "linkedin.com" in lower:
            return cls.LINKEDIN
        if "tiktok.com" in lower:
            return cls.TIKTOK
        if "facebook.com" in lower:
            return cls.FACEBOOK
        return cls.WEB


class ContentType(str, Enum):
    """What kind of content is this?"""
    POST = "post"
    THREAD = "thread"       # Twitter/X thread
    VIDEO = "video"
    ARTICLE = "article"
    IMAGE = "image"
    COMMENT = "comment"
    REEL = "reel"
    STORY = "story"
    UNKNOWN = "unknown"


class Sentiment(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    MIXED = "mixed"


# ── AI Analysis Schemas (structured output from the single LLM call) ────────────

class ContentSummary(BaseModel):
    """One-liner summary + key takeaways from the content."""
    one_liner: str = Field(
        ...,
        max_length=150,
        description="Concise 1-2 sentence summary (max 150 chars). "
                    "This is what you'd say to a friend about this content.",
    )
    key_points: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="3-5 key takeaways. Be specific, not generic.",
    )


class ContentClassification(BaseModel):
    """Thematic classification of the content."""
    primary_topics: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="Top 3 topics: tech, finance, health, design, politics, etc.",
    )
    content_type: ContentType = Field(
        default=ContentType.POST,
        description="The format/type of the content",
    )
    sentiment: Sentiment = Field(
        default=Sentiment.NEUTRAL,
        description="Overall sentiment of the content",
    )
    relevance_score: Annotated[
        int,
        Field(ge=1, le=5, description="1=low relevance to general audience, 5=must-read"),
    ] = 3
    actionability: Annotated[
        str,
        Field(pattern="^(high|medium|low|none)$", description="How actionable is this content?"),
    ] = "low"


class ExtractedEntities(BaseModel):
    """Named entities extracted from the content."""
    people: list[str] = Field(default_factory=list, max_length=20)
    organizations: list[str] = Field(default_factory=list, max_length=20)
    products: list[str] = Field(default_factory=list, max_length=20)
    technologies: list[str] = Field(default_factory=list, max_length=20)
    hashtags: list[str] = Field(default_factory=list, max_length=30)


class ContentAnalysis(BaseModel):
    """
    Complete AI analysis — the output of the ONE LLM call.

    This single model handles: summarisation, classification, entity extraction,
    sentiment, and quality scoring in a single structured response.
    Per IDEATION-CANVAS: one call to rule them all.
    """
    summary: ContentSummary
    classification: ContentClassification
    entities: ExtractedEntities
    suggested_tags: list[str] = Field(
        default_factory=list,
        max_length=15,
        description="5-10 auto-generated tags based on content themes. "
                    "These are SUGGESTIONS — user can override.",
    )
    quality_score: Annotated[
        int,
        Field(ge=1, le=5, description="Content quality/reliability: 1=spam, 5=gold"),
    ] = 3

    model_config = ConfigDict(use_enum_values=False)


# ── Database Models (what we store in Supabase) ────────────────────────────────

class ItemBase(BaseModel):
    """Fields shared by all item-related schemas."""
    url: HttpUrl
    platform: Platform
    original_id: str | None = None
    title: str | None = None
    text: str | None = None
    author: str | None = None
    author_handle: str | None = None
    author_avatar: str | None = None
    thumbnail_url: str | None = None

    @field_validator("text", mode="before")
    @classmethod
    def truncate_text(cls, v: str | None) -> str | None:
        if v and len(v) > 50_000:
            return v[:50_000] + "\n[truncated]"
        return v


class ItemCreate(ItemBase):
    """Payload for creating a new saved item."""
    # Optional: user can pre-set tags/category (AI will also suggest)
    override_tags: list[str] = Field(default_factory=list, description="Manually set tags")
    override_category: str | None = Field(None, description="Manually set category name")


class ItemUpdate(BaseModel):
    """Fields that can be updated after creation."""
    title: str | None = None
    text: str | None = None
    is_favorite: bool | None = None
    is_archived: bool | None = None
    read_at: datetime | None = None
    override_tags: list[str] | None = None
    override_category: str | None = None


class ItemAnalysis(BaseModel):
    """AI-generated enrichment data (written to Supabase alongside the item)."""
    summary: str | None = None
    key_points: list[dict] | None = None
    sentiment: str | None = None
    quality_score: int | None = None
    embedding: list[float] | None = None  # pgvector — stored as Python list
    # Structured analysis (JSONB in DB)
    analysis_json: ContentAnalysis | None = None


class Item(ItemBase, ItemAnalysis):
    """Full item as returned by the API (includes AI data + metadata)."""
    id: str
    user_id: str
    saved_at: datetime
    read_at: datetime | None = None
    is_favorite: bool = False
    is_archived: bool = False
    category_id: str | None = None
    category_name: str | None = None
    tags: list[str] = Field(default_factory=list, description="Tag names, not IDs")

    model_config = ConfigDict(from_attributes=True)


# ── Tag & Category Schemas ────────────────────────────────────────────────────

class TagBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    color: str = Field("#6366f1", pattern="^#[0-9a-fA-F]{6}$")


class TagCreate(TagBase):
    pass


class Tag(TagBase):
    id: str
    user_id: str

    model_config = ConfigDict(from_attributes=True)


class CategoryBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    color: str = Field("#10b981", pattern="^#[0-9a-fA-F]{6}$")


class CategoryCreate(CategoryBase):
    pass


class Category(CategoryBase):
    id: str
    user_id: str

    model_config = ConfigDict(from_attributes=True)


# ── Search Schemas ─────────────────────────────────────────────────────────────

class SearchQuery(BaseModel):
    """Natural language semantic search query."""
    query: str = Field(..., min_length=1, max_length=500)
    platform: Platform | None = None
    tags: list[str] = Field(default_factory=list, description="Filter by these tags")
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class SearchResult(BaseModel):
    """A single search result with relevance score."""
    item: Item
    similarity: float = Field(..., ge=0.0, le=1.0, description="Cosine similarity score")
    highlight: str | None = Field(
        None,
        description="Snipped excerpt showing where the query matched (for display)",
    )


class SearchResponse(BaseModel):
    """Paginated search results."""
    results: list[SearchResult]
    total: int
    query: str
    took_ms: float = Field(..., description="Search latency in milliseconds")


# ── Digest Schemas ─────────────────────────────────────────────────────────────

class DigestSettings(BaseModel):
    """User's digest preferences."""
    id: str
    user_id: str
    enabled: bool = True
    frequency: str = Field("weekly", pattern="^(daily|weekly|biweekly)$")
    last_sent_at: datetime | None = None


# ── Auth / User Schemas ───────────────────────────────────────────────────────

class UserProfile(BaseModel):
    id: str
    email: str
    name: str | None = None
    avatar_url: str | None = None
    created_at: datetime


class TokenResponse(BaseModel):
    """JWT returned after Supabase Auth sign-in."""
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str = "Bearer"


# ── Generic API Response Wrappers ─────────────────────────────────────────────

class ApiResponse(BaseModel):
    success: bool = True
    message: str | None = None


class PaginatedResponse(BaseModel):
    """Generic paginated list response."""
    data: list[Item]
    total: int
    page: int
    per_page: int
    has_more: bool