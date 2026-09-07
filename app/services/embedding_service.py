"""
Embedding service — generates vector embeddings.

Uses httpx directly to avoid proxy detection of OpenAI client headers.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import structlog

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
    """Build a rich embedding text that includes context beyond raw text."""
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
        text=raw_text[:6000],
        topics=topics or "general",
        hashtags=hashtags or "none",
        entities=entities or "none",
        tags=tags or "general",
    )


class EmbeddingService:
    """Generates embeddings for semantic search via httpx."""

    def __init__(self) -> None:
        cfg = get_settings()
        self._model = cfg.embedding_model
        self._dimensions = cfg.embedding_dimensions
        self._api_key = cfg.openai_api_key
        self._base_url = cfg.openai_base_url

    async def embed(self, text: str) -> list[float] | None:
        """Generate embedding. Returns None if model not available."""
        if not text.strip():
            return [0.0] * self._dimensions

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self._base_url}/embeddings",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._model,
                        "input": text[:8000],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            embedding = data["data"][0]["embedding"]
            logger.debug("Embedding generated", dims=len(embedding), model=self._model)
            return embedding
        except Exception as e:
            logger.warning("Embedding failed, skipping", error=str(e), model=self._model)
            return None

    async def embed_enriched(
        self,
        raw_text: str,
        analysis: ContentAnalysis | None = None,
        override_tags: list[str] | None = None,
    ) -> list[float] | None:
        """Generate embedding from enriched text. Returns None if model not available."""
        enriched = _build_enriched_text(raw_text, analysis, override_tags)
        return await self.embed(enriched)
