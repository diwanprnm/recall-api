"""
Auth routes — local email/password auth (replaces Supabase Auth).

Design:
  • We issue our own HS256 JWTs (app.core.security). The frontend stores the
    token and sends it as `Authorization: Bearer <jwt>` on every request.
  • Passwords are bcrypt-hashed in the `users.password_hash` column.
  • User isolation is app-enforced: routes read `user_id` from the verified JWT.
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, status
from jwt import PyJWKClient

from app.core.db import db_query, db_write
from app.core.security import (
    create_access_token,
    get_user_id_from_token,
    hash_password,
    verify_password,
)
from app.routes.deps import AuthDep, get_current_user_id
from app.schemas.schemas import (
    ApiResponse,
    AuthRequest,
    TokenResponse,
    UserProfile,
)

logger = structlog.get_logger(__name__)

# Google rotates its signing keys; PyJWKClient caches and refreshes on unknown kid.
_jwk_client = PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create account + return JWT",
)
async def register(payload: AuthRequest) -> TokenResponse:
    email = payload.email.strip().lower()
    existing = await db_query(
        "SELECT id FROM public.users WHERE email = %s", (email,)
    )
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    user_id = await db_query(
        """
        INSERT INTO public.users (email, password_hash)
        VALUES (%s, %s)
        RETURNING id
        """,
        (email, hash_password(payload.password)),
    )
    uid = user_id[0]["id"]

    # Default digest settings
    await db_write(
        "INSERT INTO public.digest_settings (user_id) VALUES (%s) "
        "ON CONFLICT (user_id) DO NOTHING",
        (uid,),
    )

    return TokenResponse(
        access_token=create_access_token(uid),
        expires_in=60 * 24 * 7 * 60,
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Sign in with email + password",
)
async def login(payload: AuthRequest) -> TokenResponse:
    rows = await db_query(
        "SELECT id, password_hash FROM public.users WHERE email = %s",
        (payload.email.strip().lower(),),
    )
    if not rows or not verify_password(payload.password, rows[0]["password_hash"] or ""):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return TokenResponse(
        access_token=create_access_token(rows[0]["id"]),
        expires_in=60 * 24 * 7 * 60,
    )



@router.get(
    "/profile",
    response_model=UserProfile,
    summary="Get current authenticated user profile",
)
async def get_profile(auth: AuthDep) -> UserProfile:
    uid = get_current_user_id(auth)
    rows = await db_query(
        "SELECT id, email, name, avatar_url, created_at FROM public.users WHERE id = %s",
        (uid,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="User not found")
    u = rows[0]
    return UserProfile(
        id=str(u["id"]),
        email=u["email"] or "",
        name=u.get("name"),
        avatar_url=u.get("avatar_url"),
        created_at=u["created_at"],
    )


@router.post(
    "/verify",
    response_model=ApiResponse,
    summary="Verify JWT validity",
)
async def verify_token(auth: AuthDep) -> ApiResponse:
    """Lightweight check that the JWT is valid + not expired."""
    token = auth.replace("Bearer ", "", 1)
    if not get_user_id_from_token(token):
        raise HTTPException(status_code=401, detail="Token invalid or expired")
    return ApiResponse(success=True, message="Token is valid")
