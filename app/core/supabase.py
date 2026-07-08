"""
Supabase client — single sync instance, lazily created.

Why SYNC, not async?
- `create_async_client` of the supabase library returns a coroutine from the factory
- For our use case (FastAPI server), sync calls inside async handlers run in
  FastAPI's thread pool — no perf cost for DB I/O
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
    """Sync anon client. Used in route handlers (with JWT attached).

    The default `create_client()` already populates the Authorization
    header with `Bearer <anon_key>` and the apikey header — both required
    by PostgREST. Setting them again with the same value causes no issues
    but adds nothing, so we just rely on defaults.
    """
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
    Sync context manager: attaches a user's JWT so PostgREST can extract
    `auth.uid()` for RLS policy evaluation.

    Supabase's `auth.set_session()` only mutates the *auth* client's session
    storage. PostgREST reads the **HTTP** headers directly. So we have to flip
    the Authorization header on the underlying client for the duration of
    the request, then restore it on exit.

    Supabase new key format uses different prefixes:
      - `sb_publishable_…` for anon key
      - `sb_secret_…` for service_role key

    Both work, just need to be passed through correctly.

    Usage in route handlers:
        with supabase_session(request.headers.get("Authorization", "")) as sb:
            result = sb.table("items").select("*").execute()
    """
    client = get_supabase_client()
    token = jwt.replace("Bearer ", "", 1) if jwt.startswith("Bearer ") else jwt
    if not token:
        yield client
        return

    # Save current Authorization header (the anon-key default)
    previous_auth = client.options.headers.get("Authorization")

    # Forward the user's JWT — PostgREST reads it and populates auth.uid()
    client.options.headers["Authorization"] = f"Bearer {token}"

    try:
        yield client
    finally:
        # Restore the anon-key Authorization header so other callers
        # (e.g. the lifespan health-check) don't see a stale user JWT.
        if previous_auth is None:
            client.options.headers.pop("Authorization", None)
        else:
            client.options.headers["Authorization"] = previous_auth


def close_supabase_clients() -> None:
    """Cleanup called on application shutdown."""
    global _client, _admin_client
    _client = None
    _admin_client = None
    logger.info("Supabase clients closed")