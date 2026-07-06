"""
Supabase client — single instance, lazily created.

Two clients are exported:
  • supabase_client   — client-facing (uses ANON key, respects RLS)
  • supabase_admin    — server-side admin (bypasses RLS, for internal ops only)
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from supabase import AsyncClient, Client, create_client

from app.core.config import get_settings

logger = structlog.get_logger(__name__)

# ── Module-level singleton (initialised once per uvicorn worker) ────────────────

_async_client: AsyncClient | None = None
_sync_client: Client | None = None


def get_supabase_client() -> AsyncClient:
    """
    Async client that uses the ANON key.
    Used in route handlers where the user's JWT is forwarded.
    """
    global _async_client
    if _async_client is None:
        cfg = get_settings()
        _async_client = create_client(
            supabase_url=cfg.supabase_url,
            supabase_key=cfg.supabase_anon_key,
        )
        logger.info("Supabase async client initialised (anon key)")
    return _async_client


def get_supabase_admin() -> Client:
    """
    Sync admin client that uses the SERVICE ROLE key.
    Bypasses RLS — NEVER expose this in route handlers!
    """
    global _sync_client
    if _sync_client is None:
        cfg = get_settings()
        _sync_client = create_client(
            supabase_url=cfg.supabase_url,
            supabase_key=cfg.supabase_service_role_key,
        )
        logger.info("Supabase admin client initialised (service role — DO NOT expose!)")
    return _sync_client


@asynccontextmanager
async def supabase_session(
    jwt: str,
) -> AsyncGenerator[AsyncClient, None]:
    """
    Context manager: attaches a user's JWT to the Supabase client so RLS
    policies are enforced.

    Usage in route handlers:
        async with supabase_session(request.headers.get("Authorization", "")):
            result = await client.table("items").select("*").execute()
    """
    client = get_supabase_client()
    # Strip "Bearer " prefix if present
    token = jwt.replace("Bearer ", "", 1) if jwt.startswith("Bearer ") else jwt
    # Set the session so RLS evaluates auth.uid() correctly
    client.auth.set_session(access_token=token, refresh_token="")
    try:
        yield client
    finally:
        # No explicit sign-out needed for single-request sessions
        pass


async def close_supabase_clients() -> None:
    """Cleanup called on application shutdown."""
    global _async_client, _sync_client
    _async_client = None
    _sync_client = None
    logger.info("Supabase clients closed")
