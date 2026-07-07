"""
Items routes — CRUD for saved content items.

Design:
  • All routes require Supabase JWT auth
  • RLS enforces user isolation (users only see their own data)
  • POST /items → extract → AI analyse → embed → store (the full pipeline)
  • PATCH /items/{id} → partial update (no re-analysis unless explicitly asked)
  • DELETE → soft archive by default, hard delete with ?hard=true
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.core.supabase import supabase_session
from app.schemas.schemas import (
    ApiResponse,
    Item,
    ItemCreate,
    ItemUpdate,
    PaginatedResponse,
)

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/items", tags=["items"])


# ── Dependency: require auth header ───────────────────────────────────────────

def _require_auth(request: Request) -> str:
    """Extract and validate Bearer token from request headers."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header. "
                   "Expected: 'Bearer <jwt>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return auth


AuthDep = Annotated[str, Depends(_require_auth)]


# ── POST /items — Save new content (full pipeline) ────────────────────────────

@router.post(
    "",
    response_model=Item,
    status_code=status.HTTP_201_CREATED,
    summary="Save new content item",
    description="""
    **The main pipeline endpoint.** This endpoint:
    1. Extracts content metadata from the URL (Open Graph, HTML meta)
    2. Runs AI analysis (summary + classification + entities + tags)
    3. Generates semantic embedding
    4. Stores everything in Supabase

    The response includes all AI-generated fields ready to display.
    """,
)
async def create_item(
    payload: ItemCreate,
    auth: AuthDep,
) -> Item:
    """
    Save a new content item with automatic AI enrichment.

    Steps:
      1. Extract content from URL (or use pre-filled payload data)
      2. Run AI analysis via 9router + instructor
      3. Generate semantic embedding
      4. Upsert item + tags + category in Supabase
    """
    from app.services.ai_service import AnalysisError
    from app.services.extraction_service import (
        ContentExtractionError,
        extract_content,
    )

    # ── Step 1: Extract content (if text not pre-filled) ────────────────────
    url_str = str(payload.url)
    if not payload.text:
        try:
            extracted = await extract_content(url_str)
            # Merge extracted data (payload takes precedence)
            payload.text = payload.text or extracted.get("text")
            payload.title = payload.title or extracted.get("title")
            payload.author = payload.author or extracted.get("author")
            payload.author_handle = payload.author_handle or extracted.get("author_handle")
            payload.author_avatar = payload.author_avatar or extracted.get("author_avatar")
            payload.thumbnail_url = payload.thumbnail_url or extracted.get("thumbnail_url")
        except ContentExtractionError:
            logger.warning("Content extraction failed, proceeding with available data", url=url_str)
    # ── Step 2: Run AI pipeline ─────────────────────────────────────────────
    from app.main import get_ai_service

    ai_svc = get_ai_service()
    raw_text = payload.text or f"{payload.title or ''} {url_str}"

    try:
        analysis, embedding = await ai_svc.analyse(
            text=raw_text,
            url=url_str,
            platform=payload.platform.value,
            title=payload.title,
            author=payload.author,
            author_handle=payload.author_handle,
            original_id=payload.original_id,
        )
    except AnalysisError:
        logger.error("AI analysis failed, saving item without enrichment", url=url_str)
        analysis, embedding = None, None

    # ── Step 3: Store in Supabase ───────────────────────────────────────────
    with supabase_session(auth) as sb:
        # Upsert item
        item_data = {
            "url": url_str,
            "platform": payload.platform.value,
            "original_id": payload.original_id,
            "title": payload.title,
            "text": payload.text,
            "author": payload.author,
            "author_handle": payload.author_handle,
            "author_avatar": payload.author_avatar,
            "thumbnail_url": payload.thumbnail_url,
            "saved_at": datetime.utcnow().isoformat(),
            "embedding": embedding,
            "analysis_json": analysis.model_dump() if analysis else None,
            "summary": analysis.summary.one_liner if analysis else None,
            "key_points": [  # JSONB array
                {"point": p} for p in (analysis.summary.key_points if analysis else [])
            ],
            "sentiment": analysis.classification.sentiment.value if analysis else None,
            "quality_score": analysis.quality_score if analysis else None,
        }
        # Filter None values (Supabase doesn't like nulls in dict literals)
        item_data = {k: v for k, v in item_data.items() if v is not None}

        resp = sb.table("items").insert(item_data).execute()

        if not resp.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create item in database",
            )

        item = resp.data[0]

        # ── Step 4: Upsert tags ─────────────────────────────────────────────
        if analysis:
            all_tags = list({*analysis.suggested_tags, *payload.override_tags})
            await _upsert_tags(sb, item["user_id"], all_tags, item["id"])

        # ── Step 5: Upsert category ─────────────────────────────────────────
        if payload.override_category and analysis:
            cat_id = _upsert_category(sb, item["user_id"], payload.override_category)
            if cat_id:
                sb.table("items").update({"category_id": cat_id}).eq("id", item["id"]).execute()

        # Refetch with related data
        return await _get_item_with_relations(sb, item["id"])


async def _upsert_tags(sb, user_id: str, tag_names: list[str], item_id: str) -> None:
    """Upsert tag records and link them to the item via ItemTag."""
    for tag_name in tag_names:
        tag_name = tag_name.strip().lower()
        if not tag_name or len(tag_name) > 50:
            continue
        # Upsert tag
        tag_resp = await sb.table("tags").upsert(
            {"user_id": user_id, "name": tag_name},
            on_conflict="user_id,name",
        ).execute()
        tag_id = tag_resp.data[0]["id"]
        # Link tag → item
        await sb.table("item_tags").upsert(
            {"item_id": item_id, "tag_id": tag_id},
            on_conflict="item_id,tag_id",
        ).execute()


async def _upsert_category(sb, user_id: str, category_name: str) -> str | None:
    """Upsert category, return its ID."""
    cat_resp = await sb.table("categories").upsert(
        {"user_id": user_id, "name": category_name.strip()},
        on_conflict="user_id,name",
    ).execute()
    return cat_resp.data[0]["id"] if cat_resp.data else None


async def _get_item_with_relations(sb, item_id: str) -> Item:
    """Fetch item with its tags and category name joined."""
    resp = await sb.table("items").select(
        "*, tags:item_tags(tag:tags(name))"
    ).eq("id", item_id).execute()
    if not resp.data:
        raise HTTPException(status_code=404, detail="Item not found")
    row = resp.data[0]
    tag_names = [
        t["tag"]["name"]
        for t in row.get("tags", [])
        if t.get("tag")
    ]
    return Item(
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
        embedding=row.get("embedding"),
        analysis_json=None,
        saved_at=row["saved_at"],
        read_at=row.get("read_at"),
        is_favorite=row.get("is_favorite", False),
        is_archived=row.get("is_archived", False),
        category_id=row.get("category_id"),
        category_name=None,
        tags=tag_names,
    )


# ── GET /items — List items with filters ────────────────────────────────────

@router.get(
    "",
    response_model=PaginatedResponse,
    summary="List saved items",
)
async def list_items(
    auth: AuthDep,
    platform: str | None = Query(None, description="Filter by platform (twitter, reddit, ...)"),
    tag: str | None = Query(None, description="Filter by tag name"),
    category_id: str | None = Query(None),
    is_favorite: bool | None = Query(None),
    is_archived: bool | None = Query(None),
    search: str | None = Query(None, description="Full-text search on title/text"),
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PaginatedResponse:
    """
    List items with server-side filtering.

    Supports filtering by platform, tag, category, favorite, archived.
    For semantic search, use POST /search instead.
    """
    with supabase_session(auth) as sb:
        query = sb.table("items").select("*, tags:item_tags(tag:tags(name))", count="exact")

        if platform:
            query = query.eq("platform", platform)
        if is_favorite is not None:
            query = query.eq("is_favorite", is_favorite)
        if is_archived is not None:
            query = query.eq("is_archived", is_archived)
        if category_id:
            query = query.eq("category_id", category_id)
        if search:
            query = query.or_(f"title.ilike.%{search}%,text.ilike.%{search}%")

        # Tag filter via join (Supabase allows nested filter)
        if tag:
            query = query.contains("tags", [{"tag": {"name": tag}}])

        query = query.order("saved_at", desc=True)
        query = query.range((page - 1) * per_page, page * per_page - 1)

        resp = query.execute()
        total = resp.count or 0

        items = []
        for row in resp.data:
            tag_names = [t["tag"]["name"] for t in row.get("tags", []) if t.get("tag")]
            items.append(Item(
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
                embedding=row.get("embedding"),
                analysis_json=None,
                saved_at=row["saved_at"],
                read_at=row.get("read_at"),
                is_favorite=row.get("is_favorite", False),
                is_archived=row.get("is_archived", False),
                category_id=row.get("category_id"),
                category_name=None,
                tags=tag_names,
            ))

        return PaginatedResponse(
            data=items,
            total=total,
            page=page,
            per_page=per_page,
            has_more=(page * per_page) < total,
        )


# ── GET /items/{id} — Get single item ──────────────────────────────────────────

@router.get(
    "/{item_id}",
    response_model=Item,
    summary="Get a single item",
)
async def get_item(auth: AuthDep, item_id: str) -> Item:
    with supabase_session(auth) as sb:
        return await _get_item_with_relations(sb, item_id)


# ── PATCH /items/{id} — Update item ───────────────────────────────────────────

@router.patch(
    "/{item_id}",
    response_model=Item,
    summary="Update an item (partial)",
)
async def update_item(
    item_id: str,
    payload: ItemUpdate,
    auth: AuthDep,
) -> Item:
    """Update item fields. Tags/category updates replace all existing tags."""
    with supabase_session(auth) as sb:
        update_data = payload.model_dump(exclude_unset=True, exclude_none=True)
        if update_data:
            sb.table("items").update(update_data).eq("id", item_id).execute()

        # If tags or category provided, replace them
        if payload.override_tags is not None:
            item_resp = sb.table("items").select("user_id").eq("id", item_id).execute()
            if item_resp.data:
                user_id = item_resp.data[0]["user_id"]
                # Remove existing tags
                sb.table("item_tags").delete().eq("item_id", item_id).execute()
                # Re-add (AI suggestions + user overrides combined)
                if payload.override_tags:
                    await _upsert_tags(sb, user_id, payload.override_tags, item_id)

        if payload.override_category:
            item_resp = sb.table("items").select("user_id").eq("id", item_id).execute()
            if item_resp.data:
                cat_id = _upsert_category(sb, item_resp.data[0]["user_id"], payload.override_category)
                if cat_id:
                    sb.table("items").update({"category_id": cat_id}).eq("id", item_id).execute()

        return await _get_item_with_relations(sb, item_id)


# ── DELETE /items/{id} — Archive or hard-delete ─────────────────────────────

@router.delete(
    "/{item_id}",
    response_model=ApiResponse,
    summary="Delete (archive) an item",
)
async def delete_item(
    item_id: str,
    auth: AuthDep,
    hard: Annotated[bool, Query(description="If true, permanently delete instead of archiving")] = False,
) -> ApiResponse:
    """Soft-delete (archive) by default. Use ?hard=true for permanent deletion."""
    with supabase_session(auth) as sb:
        if hard:
            sb.table("item_tags").delete().eq("item_id", item_id).execute()
            sb.table("items").delete().eq("id", item_id).execute()
            message = "Item permanently deleted"
        else:
            sb.table("items").update({"is_archived": True}).eq("id", item_id).execute()
            message = "Item archived (use ?hard=true to permanently delete)"
    return ApiResponse(success=True, message=message)


# ── POST /items/{id}/reanalyse — Re-run AI on existing item ───────────────────

@router.post(
    "/{item_id}/reanalyse",
    response_model=Item,
    summary="Re-run AI analysis on an existing item",
)
async def reanalyse_item(item_id: str, auth: AuthDep) -> Item:
    """
    Useful when content has been updated, or when AI model has improved.
    Re-runs the full AI pipeline and updates the item in place.
    """
    from app.services.ai_service import AnalysisError

    with supabase_session(auth) as sb:
        resp = sb.table("items").select("*").eq("id", item_id).execute()
        if not resp.data:
            raise HTTPException(status_code=404, detail="Item not found")
        item = resp.data[0]

        raw_text = f"{item.get('title', '')} {item.get('text', '')} {item.get('url', '')}"
        if not raw_text.strip():
            raise HTTPException(
                status_code=422,
                detail="Item has no text content to analyse",
            )

        from app.main import get_ai_service
        ai_svc = get_ai_service()

        try:
            analysis, embedding = await ai_svc.analyse(
                text=raw_text,
                url=item["url"],
                platform=item["platform"],
                title=item.get("title"),
                author=item.get("author"),
                original_id=item.get("original_id"),
            )
        except AnalysisError:
            raise HTTPException(
                status_code=502,
                detail="AI service temporarily unavailable. Try again later.",
            ) from None

        update_data = {
            "embedding": embedding,
            "analysis_json": analysis.model_dump(),
            "summary": analysis.summary.one_liner,
            "key_points": [{"point": p} for p in analysis.summary.key_points],
            "sentiment": analysis.classification.sentiment.value,
            "quality_score": analysis.quality_score,
        }

        sb.table("items").update(update_data).eq("id", item_id).execute()

        # Update tags
        if analysis.suggested_tags:
            sb.table("item_tags").delete().eq("item_id", item_id).execute()
            await _upsert_tags(sb, item["user_id"], analysis.suggested_tags, item_id)

        return await _get_item_with_relations(sb, item_id)
