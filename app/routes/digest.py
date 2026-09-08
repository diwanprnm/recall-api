"""
Digest routes — daily/weekly digest settings & generation.

Flow:
  1. User sets preferences (GET/PATCH /digest/settings)
  2. On trigger, call get_resurfacing_candidates RPC (now takes p_user_id)
  3. Return ranked items to re-read
"""
from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, HTTPException, Query

from app.core.db import db_query, db_write
from app.routes.deps import AuthDep, get_current_user_id
from app.schemas.schemas import (
    DigestItem,
    DigestResponse,
    DigestSettings,
    DigestSettingsUpdate,
)

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/digest", tags=["digest"])


def _settings_from_row(s: dict) -> DigestSettings:
    return DigestSettings(
        id=s["id"],
        user_id=s["user_id"],
        enabled=s["enabled"],
        frequency=s["frequency"],
        last_sent_at=s.get("last_sent_at"),
    )


async def _ensure_settings(user_id: str) -> dict:
    rows = await db_query(
        "SELECT id, user_id, enabled, frequency, last_sent_at "
        "FROM public.digest_settings WHERE user_id = %s",
        (user_id,),
    )
    if rows:
        return rows[0]
    created = await db_query(
        """
        INSERT INTO public.digest_settings (user_id, enabled, frequency)
        VALUES (%s, TRUE, 'weekly')
        RETURNING id, user_id, enabled, frequency, last_sent_at
        """,
        (user_id,),
    )
    return created[0]


# ── GET /digest/settings — Get digest preferences ───────────────────────────

@router.get("/settings", response_model=DigestSettings)
async def get_digest_settings(auth: AuthDep) -> DigestSettings:
    user_id = get_current_user_id(auth)
    return _settings_from_row(await _ensure_settings(user_id))


# ── PATCH /digest/settings — Update digest preferences ──────────────────────

@router.patch("/settings", response_model=DigestSettings)
async def update_digest_settings(
    payload: DigestSettingsUpdate, auth: AuthDep,
) -> DigestSettings:
    user_id = get_current_user_id(auth)
    update_data = payload.model_dump(exclude_unset=True, exclude_none=True)
    if not update_data:
        raise HTTPException(status_code=422, detail="No fields to update")

    set_clause = ", ".join(f"{k} = %s" for k in update_data)
    await db_write(
        f"UPDATE public.digest_settings SET {set_clause} WHERE user_id = %s",
        (*update_data.values(), user_id),
    )
    return _settings_from_row(await _ensure_settings(user_id))


# ── POST /digest/generate — Generate digest recommendations ─────────────────

@router.post("/generate", response_model=DigestResponse)
async def generate_digest(
    auth: AuthDep,
    days_since_last_read: int = Query(7, ge=1, le=90),
    limit: int = Query(20, ge=1, le=50),
) -> DigestResponse:
    """
    Generate a digest: items the user hasn't read in a while,
    ranked by quality score. Uses the get_resurfacing_candidates RPC (p_user_id).
    """
    user_id = get_current_user_id(auth)
    try:
        rows = await db_query(
            "SELECT * FROM public.get_resurfacing_candidates(%s, %s)",
            (user_id, days_since_last_read, limit),
        )
    except Exception as exc:
        logger.error("Digest RPC failed", error=str(exc))
        raise HTTPException(
            status_code=500, detail="Failed to generate digest"
        ) from None

    items = [
        DigestItem(
            id=row["id"],
            title=row.get("title"),
            summary=row.get("summary"),
            url=row["url"],
            platform=row["platform"],
            thumbnail_url=row.get("thumbnail_url"),
            saved_at=row["saved_at"],
            quality_score=row.get("quality_score"),
            days_unread=row.get("days_unread", 0),
        )
        for row in rows
    ]

    logger.info("Digest generated", count=len(items))
    return DigestResponse(
        items=items,
        total=len(items),
        generated_at=datetime.now(UTC),
    )
