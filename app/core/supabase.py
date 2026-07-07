"""
Supabase client — single sync instance, lazily created.

Why SYNC, not async?
- Supabase's `create_async_client` is itself a coroutine — adds complexity
- For our use case (FastAPI server), sync calls inside async handlers
  are perfectly fine — FastAPI runs them in a thread pool internally
- Simpler code, fewer await mistakes

Two clients are exported:
  • get_supabase_client()  — anon key, respects RLS (use in route handlers)
  • get_supabase_admin()   — service role key, bypasses RLS (use only for ops)
"""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import structlog
from supabase import Client, create_client

from app.core.config import get_settings

logger = structlog.get_logger(__name__)

# ── Module-level singleton (initialised once per uvicorn worker) ────────────────

_client: Client | None = None
_admin_client: Client | None = None


def get_supabase_client() -> Client:
    """Sync anon client. Used in route handlers (with JWT attached)."""
    global _client
    if _client is None:
        cfg = get_settings()
        _client = create_client(
            supabase_url=cfg.supabase_url,
            supabase_key=cfg.supabase_anon_key,
        )
        logger.info("Supabase anon client initialised")
    return _client


def get_supabase_admin() -> Client:
    """Sync admin (service role) client. NEVER expose in routes."""
    global _admin_client
    if _admin_client is None:
        cfg = get_settings()
        _admin_client = create_client(
            supabase_url=cfg.supabase_url,
            supabase_key=cfg.supabase_service_role_key,
        )
        logger.info("Supabase admin client initialised")
    return _admin_client


@contextmanager
def supabase_session(jwt: str) -> Generator[Client, None, None]:
    """
    Sync context manager: attaches a user's JWT to the Supabase client so RLS
    policies are enforced (`auth.uid()` resolves to that user).

    Usage in route handlers:
        with supabase_session(request.headers.get("Authorization", "")) as sb:
            result = sb.table("items").select("*").execute()
    """
    client = get_supabase_client()
    token = jwt.replace("Bearer ", "", 1) if jwt.startswith("Bearer ") else jwt
    # Set the session so RLS evaluates auth.uid() correctly
    client.auth.set_session(access_token=token, refresh_token="")
    try:
        yield client
    finally:
        # No explicit sign-out needed for single-request sessions
        pass


def close_supabase_clients() -> None:
    """Cleanup called on application shutdown."""
    global _client, _admin_client
    _client = None
    _admin_client = None
    logger.info("Supabase clients closed")