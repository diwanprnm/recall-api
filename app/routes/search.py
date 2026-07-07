"""
Semantic search route — pgvector cosine similarity via Supabase.

Key design decisions:
  1. Query embedding is generated on-the-fly (client sends natural language)
  2. Uses Supabase RPC function for vector search (avoids raw SQL in app code)
  3. Combines vector similarity + optional metadata filters
  4. Returns top-K results with similarity scores for ranking display
"""
from __future__ import annotations

import time

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.supabase import supabase_session
from app.schemas.schemas import Item, SearchQuery, SearchResponse, SearchResult

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/search", tags=["search"])


def _require_auth(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return auth


AuthDep = Depends(_require_auth)


@router.post(
    "",
    response_model=SearchResponse,
    summary="Semantic search — find items by meaning, not keywords",
    description="""
    **Semantic search endpoint.**

    Instead of keyword matching, this endpoint:
    1. Embeds your natural language query using text-embedding-3-small
    2. Searches Supabase pgvector using cosine similarity
    3. Returns ranked results with similarity scores

    Example queries:
    - "things I saved about LLM fine-tuning"
    - "marketing strategies for B2B SaaS"
    - "how to set up local development environment"

    Combine with `platform` and `tags` filters for refined results.
    """,
)
async def semantic_search(
    payload: SearchQuery,
    auth: str = AuthDep,
) -> SearchResponse:
    """
    Perform semantic search against the user's saved items.

    The query text is embedded and compared against stored item embeddings
    using cosine similarity. Results are filtered by platform/tags if provided.
    """
    from app.main import get_embedding_service

    t0 = time.monotonic()

    # ── Step 1: Embed the query ───────────────────────────────────────────────
    embedding_svc = get_embedding_service()

    try:
        query_vector = await embedding_svc.embed(payload.query)
    except Exception as exc:
        logger.error("Embedding query failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Embedding service temporarily unavailable",
        ) from None

    # ── Step 2: pgvector search via Supabase RPC ─────────────────────────────
    # We use an RPC function for the vector search to keep SQL out of the app.
    # The RPC `match_items` is defined in migration 002_schemas.sql.
    with supabase_session(auth) as sb:
        try:
            # Call the RPC function — returns items with similarity score
            rpc_result = await sb.rpc(
                "match_items",
                {
                    "query_embedding": query_vector,
                    "match_threshold": 0.7,         # minimum cosine similarity
                    "match_count": payload.limit,
                    "filter_platform": payload.platform.value if payload.platform else None,
                    "filter_tags": payload.tags if payload.tags else None,
                },
            ).execute()
        except Exception as exc:
            logger.error("Supabase RPC search failed", error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Search service temporarily unavailable",
            ) from None

        # ── Step 3: Build response ───────────────────────────────────────────
        results: list[SearchResult] = []
        for row in rpc_result.data:
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
                embedding=None,  # don't return embedding in search results
                analysis_json=None,
                saved_at=row["saved_at"],
                read_at=row.get("read_at"),
                is_favorite=row.get("is_favorite", False),
                is_archived=row.get("is_archived", False),
                category_id=row.get("category_id"),
                category_name=row.get("category_name"),
                tags=row.get("tags", []),
            )
            results.append(SearchResult(
                item=item,
                similarity=round(row.get("similarity", 0.0), 4),
                highlight=row.get("highlight") if "highlight" in row else None,
            ))

    took_ms = (time.monotonic() - t0) * 1000
    logger.info(
        "Semantic search completed",
        query=payload.query,
        results=len(results),
        took_ms=round(took_ms, 1),
    )

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
    auth: str = AuthDep,
    limit: int = 5,
) -> SearchResponse:
    """
    Find items similar to an existing saved item.
    Useful for "more like this" or knowledge graph traversal.
    """
    with supabase_session(auth) as sb:
        # Get the existing item's embedding
        resp = sb.table("items").select("id, embedding, title").eq("id", item_id).execute()
        if not resp.data:
            raise HTTPException(status_code=404, detail="Item not found")
        item = resp.data[0]
        if not item.get("embedding"):
            raise HTTPException(
                status_code=422,
                detail="This item has no embedding — try re-analysing it first",
            )

    # Re-use the semantic search with the existing item's vector
    payload = SearchQuery(query=f"related to: {item.get('title', '')}", limit=limit)
    payload.query = ""  # signal to skip embedding (we already have it)
    # Hack: store vector in a temp attribute (simpler than duplicating the search logic)
    # Instead, just call the RPC directly with the stored vector
    with supabase_session(auth) as sb:
        rpc_result = await sb.rpc(
            "match_items",
            {
                "query_embedding": item["embedding"],
                "match_threshold": 0.5,
                "match_count": limit + 1,  # +1 because result includes the item itself
                "filter_platform": None,
                "filter_tags": None,
            },
        ).execute()

    results = [
        SearchResult(
            item=Item(
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
            ),
            similarity=round(row.get("similarity", 0.0), 4),
            highlight=None,
        )
        for row in rpc_result.data
        if row["id"] != item_id  # exclude the source item itself
    ][:limit]

    return SearchResponse(
        results=results,
        total=len(results),
        query=f"related to: {item.get('title', 'this item')}",
        took_ms=0.0,
    )
