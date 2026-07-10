"""
Auth routes — Supabase Auth integration helpers.

Design:
  • We don't manage auth tokens ourselves — Supabase handles JWT issuance
  • These routes help the frontend:
      - POST /auth/verify  → validate a Supabase JWT and return user profile
      - GET  /auth/profile → get current user profile from Supabase
  • The frontend stores the Supabase JWT and sends it as Bearer token in all
    subsequent requests to the FastAPI backend
"""
from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.supabase import supabase_session
from app.core.async_supabase import execute_async
from app.schemas.schemas import ApiResponse, UserProfile

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _require_auth(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return auth


AuthDep = Annotated[str, Depends(_require_auth)]


@router.get(
    "/profile",
    response_model=UserProfile,
    summary="Get current authenticated user profile",
)
async def get_profile(auth: AuthDep) -> UserProfile:
    """
    Validates the JWT and returns the user's profile from Supabase Auth.

    The JWT is issued by Supabase after email/password or OAuth sign-in.
    We verify it server-side using Supabase's built-in JWT verification.
    """
    with supabase_session(auth) as sb:
        try:
            user = await sb.auth.get_user()
        except Exception as exc:
            logger.warning("JWT verification failed", error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

        if not user or not user.user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
            )

        return UserProfile(
            id=user.user.id,
            email=user.user.email or "",
            name=getattr(user.user, "user_metadata", {}).get("full_name"),
            avatar_url=getattr(user.user, "user_metadata", {}).get("avatar_url"),
            created_at=user.user.created_at,
        )


@router.post(
    "/verify",
    response_model=ApiResponse,
    summary="Verify JWT validity",
)
async def verify_token(auth: AuthDep) -> ApiResponse:
    """
    Lightweight health-check endpoint — just confirms the JWT is valid.
    Useful for the frontend to verify a stored session is still good.
    """
    with supabase_session(auth) as sb:
        try:
            await sb.auth.get_user()
        except Exception:
            raise HTTPException(status_code=401, detail="Token invalid or expired") from None
    return ApiResponse(success=True, message="Token is valid")


# ── Supabase Auth webhook handler (for email confirmation, etc.) ───────────────

@router.post(
    "/webhook",
    summary="Supabase Auth webhook receiver",
    description="Handles Supabase Auth events (email confirm, password reset, etc.)",
)
async def auth_webhook(request: Request) -> ApiResponse:
    """
    Receives Supabase Auth webhook events.

    Supabase sends POST requests to this endpoint on auth events.
    Configure the webhook URL in Supabase → Authentication → Webhooks.

    For now, this is a no-op placeholder. Implement specific handlers as needed:
      - user.confirmed → create UserProfile in our extensions table
      - user.deleted   → cascade delete user's data (GDPR)
    """
    body = await request.json()
    event_type = body.get("type", "unknown")
    logger.info("Auth webhook received", event=event_type)
    return ApiResponse(success=True, message=f"Webhook processed: {event_type}")
