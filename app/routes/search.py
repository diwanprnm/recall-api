"""
Semantic search route — pgvector cosine similarity (raw Postgres RPC).

Key design decisions:
  1. Query embedding is generated on-the-fly (client sends natural language)
  2. Uses the `match_items` SQL function (now takes p_user_id, no RLS)
  3. Combines vector similarity + optional metadata filters
  4. Returns top-K results with similarity scores for ranking display
"""
from __future__ import annotations

import time

import structlog
from fastapi import APIRouter, HTTPException, status
from typing import Annotated

from app.core.db import db_query
from app.routes.deps import AuthDep, get_current_user_id
from app.services import container
from app.schemas.schemas import Item, SearchQuery, SearchResponse, SearchResult

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/search", tags=["search"])


def _row_to_search_result(row: dict) -> SearchResult:
    item = Item(
        id=row["id"],
        user_id=row["user_id"],
        url=row["url"],
        platform=row["platform"],
        original_id=row.get("original_id"),
        title=row.get("title"),
        text=row.get("text"),
        author=row.get("author"),
        author_handle=row.get("author_handle"),
        author_avatar=row.get("author_avatar"),
        thumbnail_url=row.get("thumbnail_url"),
        summary=row.get("summary"),
        key_points=row.get("key_points"),
        sentiment=row.get("sentiment"),
        quality_score=row.get("quality_score"),
        embedding=None,
        analysis_json=None,
        saved_at=row["saved_at"],
        read_at=row.get("read_at"),
        is_favorite=row.get("is_favorite", False),
        is_archived=row.get("is_archived", False),
        category_id=row.get("category_id"),
        category_name=row.get("category_name"),
        tags=row.get("tags", []),
    )
    return SearchResult(
        item=item,
        similarity=round(row.get("similarity", 0.0), 4),
        highlight=row.get("highlight") if "highlight" in row else None,
    )


@router.post(
    "",
    response_model=SearchResponse,
    summary="Semantic search — find items by meaning, not keywords",
)
async def semantic_search(
    payload: SearchQuery,
    auth: AuthDep,
) -> SearchResponse:
    """Embed the query, then call match_items(p_user_id, ...) for pgvector search."""
    t0 = time.monotonic()
    user_id = get_current_user_id(auth)

    embedding_svc = container.get_embedding_service()
    try:
        query_vector = await embedding_svc.embed(payload.query)
    except Exception as exc:
        logger.error("Embedding query failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Embedding service temporarily unavailable",
        ) from None

    try:
        rows = await db_query(
            "SELECT * FROM public.match_items(%s, %s, %s, %s, %s, %s)",
            (
                user_id,
                query_vector,
                0.7,  # match_threshold
                payload.limit,  # match_count
                payload.platform.value if payload.platform else None,
                payload.tags if payload.tags else None,
            ),
        )
    except Exception as exc:
        logger.error("match_items RPC failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Search service temporarily unavailable",
        ) from None

    results = [_row_to_search_result(r) for r in rows]
    took_ms = (time.monotonic() - t0) * 1000
    logger.info("Semantic search completed", query=payload.query, results=len(results), took_ms=round(took_ms, 1))

    return SearchResponse(
        results=results,
        total=len(results),
        query=payload.query,
        took_ms=round(took_ms, 1),
    )


# ── GET /search/related/{item_id} — Find similar items ───────────────────────

@router.get(
    "/related/{item_id}",
    response_model=SearchResponse,
    summary="Find items semantically similar to a specific item",
)
async def find_related(
    item_id: str,
    auth: AuthDep,
    limit: int = 5,
) -> SearchResponse:
    """Find items similar to an existing saved item ('more like this')."""
    user_id = get_current_user_id(auth)

    rows = await db_query(
        "SELECT id, embedding, title FROM public.items WHERE id = %s AND user_id = %s",
        (item_id, user_id),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Item not found")
    item = rows[0]
    if not item.get("embedding"):
        raise HTTPException(
            status_code=422,
            detail="This item has no embedding — try re-analysing it first",
        )

    rpc_rows = await db_query(
        "SELECT * FROM public.match_items(%s, %s, %s, %s, %s, %s)",
        (user_id, item["embedding"], 0.5, limit + 1, None, None),
    )

    results = [
        _row_to_search_result(r) for r in rpc_rows if r["id"] != item_id
    ][:limit]

    return SearchResponse(
        results=results,
        total=len(results),
        query=f"related to: {item.get('title', 'this item')}",
        took_ms=0.0,
    )
