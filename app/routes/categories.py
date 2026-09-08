"""
Categories routes — CRUD for user categories.

Design:
  • All routes require a verified JWT (AuthDep)
  • User isolation is app-enforced via `user_id = %s` on every query
  • Each user has unique category names (case-insensitive via DB constraint)
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, status

from app.core.db import db_query, db_write
from app.routes.deps import AuthDep, get_current_user_id
from app.schemas.schemas import ApiResponse, Category, CategoryCreate, CategoryUpdate

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/categories", tags=["categories"])


def _category_from_row(c: dict) -> Category:
    # UUID columns come back as UUID objects; the schema wants str.
    return Category(
        id=str(c["id"]),
        user_id=str(c["user_id"]),
        name=c["name"],
        color=c["color"],
    )


# ── POST /categories — Create ───────────────────────────────────────────────

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_category(payload: CategoryCreate, auth: AuthDep) -> Category:
    user_id = get_current_user_id(auth)
    cat_name = payload.name.strip()

    existing = await db_query(
        "SELECT id FROM public.categories WHERE user_id = %s AND lower(name) = lower(%s)",
        (user_id, cat_name),
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"Category '{cat_name}' already exists")

    rows = await db_query(
        """
        INSERT INTO public.categories (user_id, name, color)
        VALUES (%s, %s, %s)
        RETURNING id, user_id, name, color
        """,
        (user_id, cat_name, payload.color),
    )
    if not rows:
        raise HTTPException(status_code=500, detail="Failed to create category")
    return _category_from_row(rows[0])


# ── GET /categories — List ──────────────────────────────────────────────────

@router.get("")
async def list_categories(auth: AuthDep) -> list[Category]:
    user_id = get_current_user_id(auth)
    rows = await db_query(
        "SELECT id, user_id, name, color FROM public.categories "
        "WHERE user_id = %s ORDER BY name ASC",
        (user_id,),
    )
    return [_category_from_row(c) for c in rows]


# ── GET /categories/{id} — Get single ───────────────────────────────────────

@router.get("/{category_id}")
async def get_category(category_id: str, auth: AuthDep) -> Category:
    user_id = get_current_user_id(auth)
    rows = await db_query(
        "SELECT id, user_id, name, color FROM public.categories "
        "WHERE id = %s AND user_id = %s",
        (category_id, user_id),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Category not found")
    return _category_from_row(rows[0])


# ── PATCH /categories/{id} — Update ─────────────────────────────────────────

@router.patch("/{category_id}")
async def update_category(
    category_id: str, payload: CategoryUpdate, auth: AuthDep,
) -> Category:
    user_id = get_current_user_id(auth)
    update_data = payload.model_dump(exclude_unset=True, exclude_none=True)
    if not update_data:
        raise HTTPException(status_code=422, detail="No fields to update")

    if "name" in update_data:
        name = update_data["name"].strip()
        existing = await db_query(
            "SELECT id FROM public.categories WHERE user_id = %s AND lower(name) = lower(%s) AND id != %s",
            (user_id, name, category_id),
        )
        if existing:
            raise HTTPException(status_code=409, detail=f"Category '{name}' already exists")
        update_data["name"] = name

    set_clause = ", ".join(f"{k} = %s" for k in update_data)
    await db_write(
        f"UPDATE public.categories SET {set_clause} WHERE id = %s AND user_id = %s",
        (*update_data.values(), category_id, user_id),
    )

    rows = await db_query(
        "SELECT id, user_id, name, color FROM public.categories WHERE id = %s AND user_id = %s",
        (category_id, user_id),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Category not found")
    return _category_from_row(rows[0])


# ── DELETE /categories/{id} — Delete ────────────────────────────────────────

@router.delete("/{category_id}")
async def delete_category(category_id: str, auth: AuthDep) -> ApiResponse:
    """Items in this category get category_id set to NULL (ON DELETE SET NULL)."""
    user_id = get_current_user_id(auth)
    await db_write(
        "DELETE FROM public.categories WHERE id = %s AND user_id = %s",
        (category_id, user_id),
    )
    return ApiResponse(success=True, message="Category deleted")
