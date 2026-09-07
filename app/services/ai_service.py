"""
AI Processing Service — THE CORE PIPELINE.

Per IDEATION-CANVAS principle: ONE LLM call does everything.
This service orchestrates a single LLM call that returns a complete
ContentAnalysis (summary + classification + entities + tags + quality score).

Then, as a separate step (after analysis is saved), we generate the embedding
from the enriched text for semantic search.
"""
from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING

import httpx
import structlog

from app.core.config import get_settings
from app.schemas.schemas import (
    ContentAnalysis,
)

if TYPE_CHECKING:
    from app.services.embedding_service import EmbeddingService

logger = structlog.get_logger(__name__)


def _extract_json(text: str) -> str:
    """Extract first complete JSON object from text that may contain preamble/epilogue."""
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]

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

Respond ONLY with valid JSON matching this schema:
{
  "summary": {"one_liner": "string max 150 chars", "key_points": ["string"]},
  "classification": {
    "primary_topics": ["string"],
    "content_type": "post|thread|video|article|image|comment|reel|story|unknown",
    "sentiment": "positive|negative|neutral|mixed",
    "relevance_score": 1-5,
    "actionability": "high|medium|low|none"
  },
  "entities": {
    "people": ["string"],
    "organizations": ["string"],
    "products": ["string"],
    "technologies": ["string"],
    "hashtags": ["string"]
  },
  "suggested_tags": ["string"],
  "quality_score": 1-5
}
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
    Uses httpx directly to avoid proxy detection of OpenAI client headers.
    """

    def __init__(
        self,
        embedding_svc: EmbeddingService,
    ) -> None:
        self._embedding = embedding_svc
        cfg = get_settings()
        self._model = cfg.ai_model
        self._api_key = cfg.openai_api_key
        self._base_url = cfg.openai_base_url

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
    ) -> tuple[ContentAnalysis, list[float] | None]:
        """
        Run the ONE LLM call to analyse content + generate embedding.

        Returns:
            (analysis, embedding_vector)
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
            model=self._model,
            base_url=self._base_url,
        )
        t0 = time.monotonic()

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                        "User-Agent": "RecallAPI/1.0",
                    },
                    json={
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": _ANALYSIS_SYSTEM_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.3,
                        "max_tokens": 8192,
                    },
                )
                logger.info("AI API response", status=resp.status_code, url=url)
                resp.raise_for_status()
                # Model response may contain control chars that break JSON
                raw_text = resp.text
                raw_text = raw_text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
                raw_text = re.sub(r'[\x00-\x1f]', ' ', raw_text)
                raw_text = re.sub(r' +', ' ', raw_text).strip()
                data = json.loads(raw_text)
            msg = data["choices"][0]["message"]
            raw = msg.get("content") or ""
            # Reasoning models put output in 'reasoning' field
            if not raw.strip():
                raw = msg.get("reasoning") or msg.get("reasoning_content") or ""
            if not raw.strip():
                raw = "{}"
            logger.info("AI raw response first 300", raw=raw[:300], url=url)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            raw = _extract_json(raw)
            # Clean control characters that break JSON parsing
            raw = raw.replace("\n", " ").replace("\r", " ").replace("\t", " ")
            raw = re.sub(r'[\x00-\x1f]', ' ', raw)
            raw = re.sub(r' +', ' ', raw).strip()
            parsed = json.loads(raw)
            analysis = ContentAnalysis.model_validate(parsed)
        except httpx.TimeoutException:
            logger.error("AI analysis timed out", url=url, model=self._model)
            raise AnalysisError(f"AI analysis timed out for {url}") from None
        except httpx.HTTPStatusError as exc:
            logger.error("AI API HTTP error", status=exc.response.status_code, body=exc.response.text[:500], url=url)
            raise AnalysisError(f"AI API error {exc.response.status_code}: {exc.response.text[:200]}") from exc
        except Exception as exc:
            logger.error("AI analysis failed", error=type(exc).__name__, detail=str(exc)[:500], url=url)
            raise AnalysisError(f"AI analysis failed: {exc}") from exc

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "AI analysis complete",
            elapsed_ms=round(elapsed_ms, 1),
            topics=analysis.classification.primary_topics,
            tags=analysis.suggested_tags[:3],
            sentiment=analysis.classification.sentiment,
        )

        # Generate embedding from enriched text
        embedding = await self._embedding.embed_enriched(
            raw_text=text,
            analysis=analysis,
        )

        return analysis, embedding

    async def suggest_title(self, text: str) -> str | None:
        """Generate a short, representative title (<=80 chars) from content.

        Used by quick-create to compress a long caption/body into a concise title
        without running the full analysis pipeline. Returns None on failure.
        """
        if not text or not text.strip():
            return None
        prompt = (
            "Write ONE concise title (max 80 characters, no quotes, no preamble) "
            "that best represents the following content. Reply with only the title.\n\n"
            f"{text[:4000]}"
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                        "User-Agent": "RecallAPI/1.0",
                    },
                    json={
                        "model": self._model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.2,
                        "max_tokens": 100,
                    },
                )
                resp.raise_for_status()
                # Model may append extra text after the JSON — extract the
                # first complete JSON object like analyse() does.
                raw_text = resp.text
                raw_text = re.sub(r'[\x00-\x1f]', ' ', raw_text)
                data = json.loads(_extract_json(raw_text))
            # `content` is the real title. `reasoning` is internal CoT — NEVER
            # use it as the title (it just echoes the prompt).
            raw = data["choices"][0]["message"].get("content") or ""
            title = raw.strip().strip('"').strip("'")
            return title[:80] if title else None
        except Exception as exc:
            logger.warning("suggest_title failed", error=str(exc))
            return None

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

    # ── Batch processing ─────────────────────────────────────────────────────

    async def analyse_batch(
        self,
        items: list[dict],
    ) -> list[tuple[ContentAnalysis, list[float] | None]]:
        """Analyse multiple items sequentially."""
        results: list[tuple[ContentAnalysis, list[float] | None]] = []
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
