"""
Supabase client — single instance, lazily created.

Two clients are exported:
  • supabase_client   — client-facing (uses ANON key, respects RLS)
  • supabase_admin    — server-side admin (bypasses RLS, for internal ops only)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from supabase import AsyncClient, create_client, Client
from supabase.lib.client_options import ClientOptions

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# ── Module-level singleton (initialised once per uvicorn worker) ────────────────

_client: AsyncClient | None = None
_admin_client: Client | None = None


def get_supabase_client() -> AsyncClient:
    """
    Async client that uses the ANON key.
    Used in route handlers where the user's JWT is forwarded.

    Usage:
        supabase = get_supabase_client()
        user = await supabase.auth.get_user(jwt)
    """
    global _client
    if _client is None:
        cfg = get_settings()
        _client = create_client(
            supabase_url=cfg.supabase_url,
            options=ClientOptions(
                auth={"autoRefreshToken": False, "persistSession": False}
            ),
        )
        # Inject anon key so we can use it in auth headers
        _client.supabase_key = cfg.supabase_anon_key  # type: ignore[attr-defined]
        logger.info("Supabase async client initialised (anon key)")
    return _client


def get_supabase_admin() -> Client:
    """
    Sync admin client that uses the SERVICE ROLE key.
    Bypasses RLS — NEVER expose this in route handlers!
    Only use for migrations, seed scripts, and internal background tasks.
    """
    global _admin_client
    if _admin_client is None:
        cfg = get_settings()
        _admin_client = create_client(
            supabase_url=cfg.supabase_url,
            options=ClientOptions(
                auth={"autoRefreshToken": False, "persistSession": False},
            ),
        )
        _admin_client.supabase_key = cfg.supabase_service_role_key  # type: ignore[attr-defined]
        logger.info("Supabase admin client initialised (service role — DO NOT expose!)")
    return _admin_client


@asynccontextmanager
async def supabase_session(
    jwt: str,
) -> AsyncGenerator[AsyncClient, None]:
    """
    Context manager: attaches a user's JWT to the Supabase client for the
    duration of the request so RLS policies are enforced.

    Usage in route handlers:
        async with supabase_session(request.headers.get("Authorization", "")):
            result = await client.table("items").select("*").execute()
    """
    client = get_supabase_client()
    # Strip "Bearer " prefix if present
    token = jwt.replace("Bearer ", "", 1) if jwt.startswith("Bearer ") else jwt
    client.auth.set_session(access_token=token, refresh_token="")
    try:
        yield client
    finally:
        client.auth.sign_out()


async def close_supabase_clients() -> None:
    """Cleanup called on application shutdown."""
    global _client, _admin_client
    if _client is not None:
        await _client.aclose()
        _client = None
        logger.info("Supabase async client closed")
    if _admin_client is not None:
        _admin_client.auth.sign_out()
        _admin_client = None
        logger.info("Supabase admin client closed")