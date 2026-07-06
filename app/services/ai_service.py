"""
AI Processing Service — THE CORE PIPELINE.

Per IDEATION-CANVAS principle: ONE LLM call does everything.
This service orchestrates a single instructor call that returns a complete
ContentAnalysis (summary + classification + entities + tags + quality score).

Then, as a separate step (after analysis is saved), we generate the embedding
from the enriched text for semantic search.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import instructor
from openai import AsyncOpenAI

from app.core.config import get_settings
from app.schemas.schemas import (
    ContentAnalysis,
    ContentClassification,
    ContentSummary,
    ExtractedEntities,
)

if TYPE_CHECKING:
    from app.services.embedding_service import EmbeddingService

logger = logging.getLogger(__name__)

# ── Single-call analysis prompt ───────────────────────────────────────────────

_ANALYSIS_SYSTEM_PROMPT = """
You are a meticulous knowledge analyst. Your job is to deeply understand
content saved from social media and extract structured metadata that makes
it easy to find, categorise, and recall later.

ANALYSIS PRINCIPLES:
- Be specific. "AI startup raises $100M" is more useful than "business news"
- Prefer concrete takeaways over vague impressions
- Quality score: 5 = highly insightful/actionable, 1 = spam/clickbait
- Actionability: "high" = reader can apply this, "none" = purely informational
- Tags should be useful for future search — not generic

Respond ONLY with valid JSON matching the schema.
""".strip()


_ANALYSIS_USER_TEMPLATE = """
Analyse the following content saved from {platform}.

Platform: {platform}
Author: {author}
Original ID: {original_id}
URL: {url}

TITLE: {title}

CONTENT:
{text}

---
Based on the above, provide a complete analysis in JSON format.
""".strip()


class AIService:
    """
    Handles all AI processing — the single-call analysis pipeline.

    Flow:
      1. prepare_prompt()  → builds the user prompt with extracted content
      2. analyse()         → ONE instructor call → ContentAnalysis
      3. generate_embedding() → creates semantic search vector

    Usage:
        svc = AIService(client, embedding_svc)
        result = await svc.analyse(user_id, url, platform, text, ...)
        # result.analysis contains summary + classification + entities + tags
        # result.embedding is ready for pgvector storage
    """

    def __init__(
        self,
        client: instructor.AsyncInstructor,
        embedding_svc: EmbeddingService,
    ) -> None:
        self._client = client
        self._embedding = embedding_svc
        self._model = get_settings().ai_model

    # ── Public API ────────────────────────────────────────────────────────────

    async def analyse(
        self,
        text: str,
        *,
        url: str,
        platform: str,
        title: str | None = None,
        author: str | None = None,
        author_handle: str | None = None,
        original_id: str | None = None,
    ) -> tuple[ContentAnalysis, list[float]]:
        """
        Run the ONE LLM call to analyse content + generate embedding.

        Returns:
            (analysis, embedding_vector)

        Raises:
            AnalysisError: if the LLM call fails after retries
        """
        user_prompt = self._build_prompt(
            text=text,
            url=url,
            platform=platform,
            title=title,
            author=author,
            author_handle=author_handle,
            original_id=original_id,
        )

        logger.info(
            "Starting AI analysis",
            platform=platform,
            text_chars=len(text),
            title=title,
        )
        t0 = time.monotonic()

        try:
            analysis = await self._client.messages.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _ANALYSIS_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                response_model=ContentAnalysis,
                max_retries=2,
                # temperature=0.3 → focused, deterministic analysis
                temperature=0.3,
            )
        except Exception as exc:
            logger.error("AI analysis failed", error=str(exc), url=url)
            raise AnalysisError(f"AI analysis failed: {exc}") from exc

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "AI analysis complete",
            elapsed_ms=round(elapsed_ms, 1),
            topics=analysis.classification.primary_topics,
            tags=analysis.suggested_tags[:3],
            sentiment=analysis.classification.sentiment,
        )

        # Generate embedding from enriched text (separate call, done after analysis is stored)
        embedding = await self._embedding.embed_enriched(
            raw_text=text,
            analysis=analysis,
        )

        return analysis, embedding

    # ── Helper methods ────────────────────────────────────────────────────────

    def _build_prompt(
        self,
        *,
        text: str,
        url: str,
        platform: str,
        title: str | None = None,
        author: str | None = None,
        author_handle: str | None = None,
        original_id: str | None = None,
    ) -> str:
        """Build the user prompt with all available context."""
        return _ANALYSIS_USER_TEMPLATE.format(
            platform=platform,
            url=url,
            title=title or "(no title)",
            author=author or "(unknown author)",
            author_handle=author_handle or "(no handle)",
            original_id=original_id or "(none)",
            text=text or "(no text content — check URL metadata)",
        )

    # ── Batch processing (for future digest / bulk re-analysis) ───────────────

    async def analyse_batch(
        self,
        items: list[dict],
    ) -> list[tuple[ContentAnalysis, list[float]]]:
        """
        Analyse multiple items sequentially.
        For true batch processing (cheaper + faster), use OpenAI Batch API
        with a JSONL file — but that adds complexity. Start with sequential.
        """
        results: list[tuple[ContentAnalysis, list[float]]] = []
        for item in items:
            try:
                result = await self.analyse(
                    text=item["text"],
                    url=item["url"],
                    platform=item["platform"],
                    title=item.get("title"),
                    author=item.get("author"),
                )
                results.append(result)
            except AnalysisError:
                logger.warning("Skipping failed item in batch", url=item["url"])
                continue
        return results


# ── Exception ─────────────────────────────────────────────────────────────────

class AnalysisError(Exception):
    """Raised when the AI analysis pipeline fails after all retries."""
    pass