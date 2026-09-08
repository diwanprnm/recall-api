"""
Items routes — CRUD for saved content items.

Design:
  • All routes require a verified JWT (AuthDep)
  • User isolation is APP-ENFORCED: every query includes `user_id = %s`
    (no RLS — the route supplies the authenticated user id from the JWT)
  • POST /items → extract → AI analyse → embed → store (the full pipeline)
  • PATCH /items/{id} → partial update (no re-analysis unless explicitly asked)
  • DELETE → soft archive by default, hard delete with ?hard=true
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, Query, status

from app.core.db import db_query, db_write
from app.routes.deps import AuthDep, get_current_user_id
from app.schemas.schemas import (
    ApiResponse,
    Item,
    ItemCreate,
    ItemUpdate,
    PaginatedResponse,
)
from app.services import container

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/items", tags=["items"])

# Explicit columns for list reads — excludes `embedding` (1536-float pgvector,
# unused by the list view; shipping it per row is the main list latency cost).
_LIST_COLS = (
    "id, user_id, url, platform, original_id, title, text, author, author_handle, "
    "author_avatar, thumbnail_url, summary, key_points, sentiment, quality_score, "
    "saved_at, read_at, is_favorite, is_archived, category_id"
)


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _category_name(sb_user_id: str, category_id: str | None) -> str | None:
    if not category_id:
        return None
    rows = await db_query(
        "SELECT name FROM public.categories WHERE id = %s AND user_id = %s",
        (category_id, sb_user_id),
    )
    return rows[0]["name"] if rows else None


async def _resolve_category_id(user_id: str, override_category: str) -> str | None:
    """Resolve a category from the frontend value.

    The frontend sends either a category UUID (reuse existing) or a raw name
    (upsert a new one). Create and update paths must agree on this.
    """
    try:
        cat_id = str(UUID(override_category))
    except ValueError:
        rows = await db_query(
            "SELECT id FROM public.categories WHERE user_id = %s AND lower(name) = lower(%s)",
            (user_id, override_category.strip()),
        )
        if rows:
            return rows[0]["id"]
        created = await db_query(
            "INSERT INTO public.categories (user_id, name) VALUES (%s, %s) RETURNING id",
            (user_id, override_category.strip().lower()),
        )
        return created[0]["id"] if created else None
    # It's a UUID — verify ownership
    rows = await db_query(
        "SELECT id FROM public.categories WHERE id = %s AND user_id = %s",
        (cat_id, user_id),
    )
    return rows[0]["id"] if rows else None


async def _upsert_tags(user_id: str, tag_names: list[str], item_id: str) -> None:
    """Upsert tag records and link them to the item via item_tags."""
    for tag_name in tag_names:
        tag_name = tag_name.strip().lower()
        if not tag_name or len(tag_name) > 50:
            continue
        rows = await db_query(
            "SELECT id FROM public.tags WHERE user_id = %s AND lower(name) = lower(%s)",
            (user_id, tag_name),
        )
        if rows:
            tag_id = rows[0]["id"]
        else:
            created = await db_query(
                "INSERT INTO public.tags (user_id, name) VALUES (%s, %s) RETURNING id",
                (user_id, tag_name),
            )
            tag_id = created[0]["id"]
        await db_write(
            "INSERT INTO public.item_tags (item_id, tag_id) VALUES (%s, %s) "
            "ON CONFLICT (item_id, tag_id) DO NOTHING",
            (item_id, tag_id),
        )


async def _item_with_relations(item_id: str, user_id: str) -> Item:
    """Fetch one item with its tags + category name."""
    rows = await db_query(
        f"SELECT {_LIST_COLS}, "
        "ARRAY(SELECT t.name FROM public.item_tags it "
        "JOIN public.tags t ON t.id = it.tag_id WHERE it.item_id = items.id) AS tags "
        "FROM public.items WHERE id = %s AND user_id = %s",
        (item_id, user_id),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Item not found")
    row = rows[0]
    cat_name = await _category_name(user_id, row.get("category_id"))
    return _row_to_item(row, cat_name)


def _row_to_item(row: dict, category_name: str | None = None) -> Item:
    # psycopg returns UUID columns as UUID objects; the API schema wants str.
    def _s(v):
        return str(v) if v is not None else None

    # key_points is JSONB: an empty/default can read as {} (dict) not [].
    kp = row.get("key_points")
    if not isinstance(kp, list):
        kp = []

    return Item(
        id=_s(row["id"]),
        user_id=_s(row["user_id"]),
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
        key_points=kp,
        sentiment=row.get("sentiment"),
        quality_score=row.get("quality_score"),
        embedding=None,
        analysis_json=None,
        saved_at=row["saved_at"],
        read_at=row.get("read_at"),
        is_favorite=row.get("is_favorite", False),
        is_archived=row.get("is_archived", False),
        category_id=_s(row.get("category_id")),
        category_name=category_name,
        tags=row.get("tags") or [],
    )


# ── POST /items — Save new content (full pipeline) ────────────────────────────

@router.post(
    "",
    response_model=Item,
    status_code=status.HTTP_201_CREATED,
    summary="Save new content item",
)
async def create_item(
    payload: ItemCreate,
    auth: AuthDep,
) -> Item:
    """Save a new content item with automatic AI enrichment."""
    from app.services.ai_service import AnalysisError
    from app.services.extraction_service import (
        ContentExtractionError,
        extract_content,
    )

    user_id = get_current_user_id(auth)
    url_str = str(payload.url)

    # ── Step 1: Extract content (if text not pre-filled) ────────────────────
    if not payload.text:
        try:
            extracted = await extract_content(url_str)
            payload.text = payload.text or extracted.get("text")
            payload.title = payload.title or extracted.get("title")
            payload.author = payload.author or extracted.get("author")
            payload.author_handle = payload.author_handle or extracted.get("author_handle")
            payload.author_avatar = payload.author_avatar or extracted.get("author_avatar")
            payload.thumbnail_url = payload.thumbnail_url or extracted.get("thumbnail_url")
        except ContentExtractionError:
            logger.warning("Content extraction failed, proceeding with available data", url=url_str)

    # ── Step 2: Run AI pipeline ─────────────────────────────────────────────
    ai_svc = container.get_ai_service()
    raw_text = payload.text or f"{payload.title or ''} {url_str}"

    analysis, embedding = None, None
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

    # ── Step 3: Store in Postgres ───────────────────────────────────────────
    insert_sql = """
        INSERT INTO public.items
            (user_id, url, platform, original_id, title, text, author, author_handle,
             author_avatar, thumbnail_url, saved_at, embedding, analysis_json, summary,
             key_points, sentiment, quality_score)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, url) DO UPDATE
            SET title = EXCLUDED.title, text = EXCLUDED.text,
                analysis_json = EXCLUDED.analysis_json, summary = EXCLUDED.summary,
                key_points = EXCLUDED.key_points, sentiment = EXCLUDED.sentiment,
                quality_score = EXCLUDED.quality_score, embedding = EXCLUDED.embedding,
                updated_at = now()
        RETURNING id
    """
    saved_at = datetime.now(UTC).isoformat()
    rows = await db_query(
        insert_sql,
        (
            user_id, url_str, payload.platform.value, payload.original_id,
            payload.title, payload.text, payload.author, payload.author_handle,
            payload.author_avatar, payload.thumbnail_url, saved_at,
            embedding, analysis.model_dump() if analysis else None,
            analysis.summary.one_liner if analysis else None,
            [{"point": p} for p in (analysis.summary.key_points if analysis else [])],
            analysis.classification.sentiment.value if analysis else None,
            analysis.quality_score if analysis else None,
        ),
    )
    if not rows:
        raise HTTPException(status_code=500, detail="Failed to create item in database")
    item_id = rows[0]["id"]

    # ── Step 4: Upsert tags ─────────────────────────────────────────────────
    if analysis:
        all_tags = list({*analysis.suggested_tags, *payload.override_tags})
        await _upsert_tags(user_id, all_tags, item_id)

    # ── Step 5: Upsert category ─────────────────────────────────────────────
    if payload.override_category:
        cat_id = await _resolve_category_id(user_id, payload.override_category)
        if cat_id:
            await db_write(
                "UPDATE public.items SET category_id = %s WHERE id = %s AND user_id = %s",
                (cat_id, item_id, user_id),
            )

    return await _item_with_relations(item_id, user_id)


# ── POST /items/quick — Save without AI analysis ─────────────────────────────

@router.post(
    "/quick",
    response_model=Item,
    status_code=status.HTTP_201_CREATED,
    summary="Save item without AI enrichment (no analysis/embedding)",
)
async def create_item_quick(
    payload: ItemCreate,
    auth: AuthDep,
) -> Item:
    """Save a new content item — same as POST /items but skips the AI pipeline.

    Content extraction still runs if text isn't pre-filled; only the
    analysis/embedding step is omitted.
    """
    from app.services.extraction_service import (
        ContentExtractionError,
        extract_content,
    )

    user_id = get_current_user_id(auth)
    url_str = str(payload.url)

    # ── Step 1: Extract content (if text not pre-filled) ────────────────────
    if not payload.text:
        try:
            extracted = await extract_content(url_str)
            payload.text = payload.text or extracted.get("text")
            payload.title = payload.title or extracted.get("title")
            payload.author = payload.author or extracted.get("author")
            payload.author_handle = payload.author_handle or extracted.get("author_handle")
            payload.author_avatar = payload.author_avatar or extracted.get("author_avatar")
            payload.thumbnail_url = payload.thumbnail_url or extracted.get("thumbnail_url")
        except ContentExtractionError:
            logger.warning("Content extraction failed, proceeding with available data", url=url_str)

    # ── Step 2: AI title only (no full analysis / embedding) ────────────────
    # Use AI to derive a short representative title when missing or too long.
    # `text` is left as-is (may be empty for IG public pages — never fall back
    # to the title, since that would duplicate content).
    if not payload.title or len(payload.title) > 80:
        ai_svc = container.get_ai_service()
        suggested = await ai_svc.suggest_title(payload.text or payload.title or "")
        if suggested:
            payload.title = suggested

    # ── Step 3: Store in Postgres (no embedding / analysis columns) ──────────
    insert_sql = """
        INSERT INTO public.items
            (user_id, url, platform, original_id, title, text, author, author_handle,
             author_avatar, thumbnail_url, saved_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, url) DO UPDATE
            SET title = EXCLUDED.title, text = EXCLUDED.text, updated_at = now()
        RETURNING id
    """
    saved_at = datetime.now(UTC).isoformat()
    rows = await db_query(
        insert_sql,
        (
            user_id, url_str, payload.platform.value, payload.original_id,
            payload.title, payload.text, payload.author, payload.author_handle,
            payload.author_avatar, payload.thumbnail_url, saved_at,
        ),
    )
    if not rows:
        raise HTTPException(status_code=500, detail="Failed to create item in database")
    item_id = rows[0]["id"]

    # ── Step 4: Upsert tags ─────────────────────────────────────────────────
    if payload.override_tags:
        await _upsert_tags(user_id, payload.override_tags, item_id)

    # ── Step 5: Upsert category ─────────────────────────────────────────────
    if payload.override_category:
        cat_id = await _resolve_category_id(user_id, payload.override_category)
        if cat_id:
            await db_write(
                "UPDATE public.items SET category_id = %s WHERE id = %s AND user_id = %s",
                (cat_id, item_id, user_id),
            )

    return await _item_with_relations(item_id, user_id)


# ── GET /items/counts — Stable counts for filter badges ─────────────────────

@router.get("/counts", summary="Get counts per platform and category")
async def get_item_counts(
    auth: AuthDep,
    platform: str | None = Query(None),
    category_id: str | None = Query(None),
) -> dict:
    """Return total, per-platform, and per-category counts (excludes archived)."""
    user_id = get_current_user_id(auth)
    where = "WHERE user_id = %s AND is_archived = FALSE"
    params: list = [user_id]
    if platform:
        where += " AND platform = %s"
        params.append(platform)
    if category_id:
        where += " AND category_id = %s"
        params.append(category_id)

    rows = await db_query(f"SELECT platform, category_id FROM public.items {where}", params)
    total = len(rows)

    platform_counts: dict[str, int] = {}
    for row in rows:
        p = row["platform"]
        platform_counts[p] = platform_counts.get(p, 0) + 1

    raw_cat_ids = list({row["category_id"] for row in rows if row.get("category_id")})
    cat_map: dict[str, str] = {}
    if raw_cat_ids:
        crows = await db_query(
            "SELECT id, name FROM public.categories WHERE id = ANY(%s) AND user_id = %s",
            (raw_cat_ids, user_id),
        )
        cat_map = {c["id"]: c["name"] for c in crows}

    category_counts: dict[str, int] = {}
    for row in rows:
        cid = row.get("category_id")
        if cid:
            name = cat_map.get(cid, "Unknown")
            category_counts[name] = category_counts.get(name, 0) + 1

    return {
        "total": total,
        "by_platform": platform_counts,
        "by_category": category_counts,
    }


# ── GET /items — List items with filters ────────────────────────────────────

@router.get(
    "",
    response_model=PaginatedResponse,
    summary="List saved items",
)
async def list_items(
    auth: AuthDep,
    platform: str | None = Query(None, description="Filter by platform"),
    tag: str | None = Query(None, description="Filter by tag name"),
    category_id: str | None = Query(None),
    is_favorite: bool | None = Query(None),
    is_archived: bool | None = Query(None),
    search: str | None = Query(None, description="Full-text search on title/text"),
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PaginatedResponse:
    """List items with server-side filtering (app-enforced user isolation)."""
    user_id = get_current_user_id(auth)

    params: list = [user_id]
    conds = ["user_id = %s"]
    if platform:
        conds.append("platform = %s")
        params.append(platform)
    if is_favorite is not None:
        conds.append("is_favorite = %s")
        params.append(is_favorite)
    if is_archived is not None:
        conds.append("is_archived = %s")
        params.append(is_archived)
    if category_id:
        conds.append("category_id = %s")
        params.append(category_id)
    if search:
        conds.append("(title ILIKE %s OR text ILIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])
    if tag:
        conds.append(
            "id IN (SELECT it.item_id FROM public.item_tags it "
            "JOIN public.tags t ON t.id = it.tag_id WHERE t.name = %s)"
        )
        params.append(tag)

    where = " AND ".join(conds)
    offset = (page - 1) * per_page
    params.extend([per_page, offset])

    # Total count (separate, cheap)
    total_rows = await db_query(
        f"SELECT COUNT(*) AS c FROM public.items WHERE {where}", params[: len(params) - 2]
    )
    total = total_rows[0]["c"] if total_rows else 0

    rows = await db_query(
        f"SELECT {_LIST_COLS}, "
        "ARRAY(SELECT t.name FROM public.item_tags it "
        "JOIN public.tags t ON t.id = it.tag_id WHERE it.item_id = items.id) AS tags "
        f"FROM public.items WHERE {where} "
        "ORDER BY saved_at DESC LIMIT %s OFFSET %s",
        params,
    )

    # Batch category names
    cat_ids = list({r["category_id"] for r in rows if r.get("category_id")})
    cat_map: dict[str, str] = {}
    if cat_ids:
        crows = await db_query(
            "SELECT id, name FROM public.categories WHERE id = ANY(%s) AND user_id = %s",
            (cat_ids, user_id),
        )
        cat_map = {c["id"]: c["name"] for c in crows}

    items = [_row_to_item(r, cat_map.get(r["category_id"])) for r in rows]
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
    user_id = get_current_user_id(auth)
    return await _item_with_relations(item_id, user_id)


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
    """Update item fields. Tags/category replacements handled separately."""
    user_id = get_current_user_id(auth)

    update_data = payload.model_dump(exclude_unset=True, exclude_none=True)
    update_data.pop("override_tags", None)
    update_data.pop("override_category", None)

    if update_data:
        set_clause = ", ".join(f"{k} = %s" for k in update_data)
        await db_write(
            f"UPDATE public.items SET {set_clause}, updated_at = now() "
            "WHERE id = %s AND user_id = %s",
            (*update_data.values(), item_id, user_id),
        )

    if payload.override_tags is not None:
        await db_write("DELETE FROM public.item_tags WHERE item_id = %s", (item_id,))
        if payload.override_tags:
            await _upsert_tags(user_id, payload.override_tags, item_id)

    if payload.override_category:
        cat_id = await _resolve_category_id(user_id, payload.override_category)
        if cat_id:
            await db_write(
                "UPDATE public.items SET category_id = %s WHERE id = %s AND user_id = %s",
                (cat_id, item_id, user_id),
            )

    return await _item_with_relations(item_id, user_id)


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
    user_id = get_current_user_id(auth)
    if hard:
        await db_write("DELETE FROM public.item_tags WHERE item_id = %s", (item_id,))
        await db_write(
            "DELETE FROM public.items WHERE id = %s AND user_id = %s", (item_id, user_id)
        )
        message = "Item permanently deleted"
    else:
        await db_write(
            "UPDATE public.items SET is_archived = TRUE, updated_at = now() "
            "WHERE id = %s AND user_id = %s",
            (item_id, user_id),
        )
        message = "Item archived (use ?hard=true to permanently delete)"
    return ApiResponse(success=True, message=message)


# ── POST /items/{id}/reanalyse — Re-run AI on existing item ───────────────────

@router.post(
    "/{item_id}/reanalyse",
    response_model=Item,
    summary="Re-run AI analysis on an existing item",
)
async def reanalyse_item(item_id: str, auth: AuthDep) -> Item:
    """Re-runs the full AI pipeline and updates the item in place."""
    from app.services import container
    from app.services.ai_service import AnalysisError

    user_id = get_current_user_id(auth)
    rows = await db_query(
        "SELECT title, text, url, platform FROM public.items WHERE id = %s AND user_id = %s",
        (item_id, user_id),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Item not found")
    item = rows[0]

    raw_text = f"{item.get('title', '')} {item.get('text', '')} {item.get('url', '')}"
    if not raw_text.strip():
        raise HTTPException(status_code=422, detail="Item has no text content to analyse")

    ai_svc = container.get_ai_service()
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
            status_code=502, detail="AI service temporarily unavailable. Try again later."
        ) from None

    await db_write(
        "UPDATE public.items SET embedding = %s, analysis_json = %s, summary = %s, "
        "key_points = %s, sentiment = %s, quality_score = %s, updated_at = now() "
        "WHERE id = %s AND user_id = %s",
        (
            embedding,
            analysis.model_dump(),
            analysis.summary.one_liner,
            [{"point": p} for p in analysis.summary.key_points],
            analysis.classification.sentiment.value,
            analysis.quality_score,
            item_id,
            user_id,
        ),
    )

    if analysis.suggested_tags:
        await db_write("DELETE FROM public.item_tags WHERE item_id = %s", (item_id,))
        await _upsert_tags(user_id, analysis.suggested_tags, item_id)

    return await _item_with_relations(item_id, user_id)


# ── POST /items/{id}/favorite — Toggle favorite ─────────────────────────────

@router.post(
    "/{item_id}/favorite",
    response_model=Item,
    summary="Toggle favorite on/off",
)
async def toggle_favorite(item_id: str, auth: AuthDep) -> Item:
    """Flip is_favorite boolean. Returns the updated item."""
    user_id = get_current_user_id(auth)
    rows = await db_query(
        "SELECT is_favorite FROM public.items WHERE id = %s AND user_id = %s",
        (item_id, user_id),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Item not found")
    new_val = not rows[0]["is_favorite"]
    await db_write(
        "UPDATE public.items SET is_favorite = %s, updated_at = now() WHERE id = %s AND user_id = %s",
        (new_val, item_id, user_id),
    )
    return await _item_with_relations(item_id, user_id)


# ── POST /items/{id}/archive — Toggle archive ───────────────────────────────

@router.post(
    "/{item_id}/archive",
    response_model=Item,
    summary="Toggle archive on/off",
)
async def toggle_archive(item_id: str, auth: AuthDep) -> Item:
    """Flip is_archived boolean. Returns the updated item."""
    user_id = get_current_user_id(auth)
    rows = await db_query(
        "SELECT is_archived FROM public.items WHERE id = %s AND user_id = %s",
        (item_id, user_id),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Item not found")
    new_val = not rows[0]["is_archived"]
    await db_write(
        "UPDATE public.items SET is_archived = %s, updated_at = now() WHERE id = %s AND user_id = %s",
        (new_val, item_id, user_id),
    )
    return await _item_with_relations(item_id, user_id)
