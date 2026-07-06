"""
Embedding service — generates vector embeddings via 9router.

Embedding is the foundation of semantic search. We embed:
  1. Raw text content (for basic similarity)
  2. Enriched text (with entities, topics, tags) — 3x better quality

Uses text-embedding-3-small (1536 dimensions, $0.00002/1K tokens).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from openai import AsyncOpenAI

from app.core.config import get_settings

if TYPE_CHECKING:
    from app.schemas.schemas import ContentAnalysis

logger = structlog.get_logger(__name__)

# ── Prompt templates for enriched embeddings ─────────────────────────────────

_ENRICHED_EMBEDDING_TEMPLATE = """
CONTENT: {text}

TOPICS: {topics}
HASHTAGS: {hashtags}
ENTITIES: {entities}

TAGS: {tags}
""".strip()


def _build_enriched_text(
    raw_text: str,
    analysis: ContentAnalysis | None,
    override_tags: list[str] | None = None,
) -> str:
    """
    Build a rich embedding text that includes context beyond raw text.
    This is the key to 3x better semantic search per IDEATION-CANVAS.
    """
    if not analysis:
        return raw_text[:8000]

    topics = ", ".join(analysis.classification.primary_topics)
    hashtags = ", ".join(analysis.entities.hashtags)
    entities = ", ".join(
        analysis.entities.people
        + analysis.entities.organizations
        + analysis.entities.technologies
    )
    all_tags = list(
        {*analysis.suggested_tags, *(override_tags or [])}
    )
    tags = ", ".join(all_tags)

    return _ENRICHED_EMBEDDING_TEMPLATE.format(
        text=raw_text[:6000],   # leave room for context
        topics=topics or "general",
        hashtags=hashtags or "none",
        entities=entities or "none",
        tags=tags or "general",
    )


class EmbeddingService:
    """
    Generates embeddings for semantic search.

    Usage:
        svc = EmbeddingService()
        vector = await svc.embed("Your brilliant idea here")
        enriched = await svc.embed_enriched(raw_text, analysis, tags=["ai", "startup"])
    """

    def __init__(self, client: AsyncOpenAI) -> None:
        self._client = client
        cfg = get_settings()
        self._model = cfg.embedding_model
        self._dimensions = cfg.embedding_dimensions

    @property
    def client(self) -> AsyncOpenAI:
        return self._client

    async def embed(self, text: str) -> list[float]:
        """
        Generate a single embedding vector from plain text.
        Used for real-time search query embedding.
        """
        if not text.strip():
            # Return zero vector for empty input
            return [0.0] * self._dimensions

        response = await self._client.embeddings.create(
            model=self._model,
            input=text[:8000],   # token limit safety
            dimensions=self._dimensions,
        )
        embedding = response.data[0].embedding
        logger.debug("Embedding generated", dims=len(embedding), model=self._model)
        return embedding

    async def embed_enriched(
        self,
        raw_text: str,
        analysis: ContentAnalysis | None = None,
        override_tags: list[str] | None = None,
    ) -> list[float]:
        """
        Generate embedding from enriched text (text + entities + tags + topics).

        This creates vectors that capture not just WHAT the content says,
        but WHAT IT'S ABOUT — dramatically improving semantic search quality.
        Called during item creation after AI analysis is complete.
        """
        enriched = _build_enriched_text(raw_text, analysis, override_tags)
        return await self.embed(enriched)

    # ── Batch embedding (for future use with many items) ─────────────────────

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple texts in one API call.
        9router supports batch — use this when processing multiple items.
        """
        # Chunk into 2048 items per call (OpenAI batch limit)
        all_embeddings: list[list[float]] = []
        for chunk in [texts[i : i + 2048] for i in range(0, len(texts), 2048)]:
            response = await self._client.embeddings.create(
                model=self._model,
                input=[t[:8000] for t in chunk],
                dimensions=self._dimensions,
            )
            all_embeddings.extend(item.embedding for item in response.data)

        logger.info("Batch embeddings generated", count=len(texts))
        return all_embeddings
