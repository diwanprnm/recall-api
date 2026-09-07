"""
Tags routes — CRUD for user tags.

Design:
  • All routes require a verified JWT (AuthDep)
  • User isolation is app-enforced via `user_id = %s` on every query
  • Tags are unique per user (case-insensitive via DB constraint)
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, status

from app.core.db import db_query, db_write
from app.routes.deps import AuthDep, get_current_user_id
from app.schemas.schemas import Tag, TagCreate, ApiResponse

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/tags", tags=["tags"])


# ── POST /tags — Create a new tag ───────────────────────────────────────────

@router.post(
    "",
    response_model=Tag,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new tag",
)
async def create_tag(payload: TagCreate, auth: AuthDep) -> Tag:
    """Create a new tag for the authenticated user. Name must be unique per user."""
    user_id = get_current_user_id(auth)
    tag_name = payload.name.strip().lower()

    existing = await db_query(
        "SELECT id FROM public.tags WHERE user_id = %s AND lower(name) = lower(%s)",
        (user_id, tag_name),
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tag '{tag_name}' already exists",
        )

    rows = await db_query(
        """
        INSERT INTO public.tags (user_id, name, color)
        VALUES (%s, %s, %s)
        RETURNING id, user_id, name, color
        """,
        (user_id, tag_name, payload.color),
    )
    if not rows:
        raise HTTPException(status_code=500, detail="Failed to create tag")

    t = rows[0]
    return Tag(id=t["id"], user_id=t["user_id"], name=t["name"], color=t["color"])


# ── GET /tags — List all tags for current user ──────────────────────────────

@router.get(
    "",
    response_model=list[Tag],
    summary="List all tags",
)
async def list_tags(auth: AuthDep) -> list[Tag]:
    """List all tags owned by the authenticated user."""
    user_id = get_current_user_id(auth)
    rows = await db_query(
        "SELECT id, user_id, name, color FROM public.tags "
        "WHERE user_id = %s ORDER BY name ASC",
        (user_id,),
    )
    return [Tag(id=t["id"], user_id=t["user_id"], name=t["name"], color=t["color"]) for t in rows]
