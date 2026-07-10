"""
Supabase client — thread-safe, per-request sessions.

Why SYNC, not async?
- `create_async_client` of the supabase library returns a coroutine from the factory
- For our use case (FastAPI server), sync calls inside async handlers run in
  FastAPI's thread pool — no perf cost for DB I/O
- Simpler code, fewer await mistakes

Two clients are exported:
  • get_supabase_client()  — anon key, respects RLS (use in route handlers)
  • get_supabase_admin()   — service role key, bypasses RLS (use only for ops)

Thread-safety:
  Each request gets its own supabase_session() context that creates a CLIENT
  COPY per-invocation, avoiding race conditions when multiple async requests
  share the same event loop.
"""
from __future__ import annotations

import copy
import threading
from collections.abc import Generator
from contextlib import contextmanager

import structlog
from supabase import Client, create_client

from app.core.config import get_settings

logger = structlog.get_logger(__name__)

# ── Module-level singletons (initialised once per uvicorn worker) ──────────────

_client: Client | None = None
_admin_client: Client | None = None
_lock = threading.Lock()


def _init_client() -> None:
    """Lazy initialise the anon client once per worker."""
    global _client
    if _client is None:
        cfg = get_settings()
        _client = create_client(
            supabase_url=cfg.supabase_url,
            supabase_key=cfg.supabase_anon_key,
        )
        logger.info("Supabase anon client initialised")


def get_supabase_client() -> Client:
    """Sync anon client that uses the ANON key + respects RLS."""
    _init_client()
    return _client  # type: ignore[return-value]


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


def _copy_client(base: Client) -> Client:
    """
    Create a lightweight copy of a Supabase client for per-request use.

    This avoids mutating the shared singleton's headers, which would cause
    race conditions under concurrent async requests.
    """
    new_client = copy.copy(base)
    # Ensure the copy has its own mutable headers dict
    new_client.options = copy.copy(new_client.options)
    new_client.options.headers = copy.copy(new_client.options.headers)
    return new_client


@contextmanager
def supabase_session(jwt: str) -> Generator[Client, None, None]:
    """
    Per-request context manager: creates a CLIENT COPY with the user's JWT
    so PostgREST can extract `auth.uid()` for RLS policy evaluation.

    Each concurrent request gets its own client copy — no shared mutable state.

    Usage:
        with supabase_session(request.headers.get("Authorization", "")) as sb:
            items = sb.table("items").select("*").execute()
    """
    _init_client()
    base_client = _client  # type: ignore[union-attr]

    token = jwt.replace("Bearer ", "", 1) if jwt.startswith("Bearer ") else jwt
    if not token:
        logger.warning("No JWT token provided to supabase_session — RLS will use anon key")
        yield base_client
        return

    # Create a per-request client copy to avoid race conditions
    client = _copy_client(base_client)
    client.options.headers["Authorization"] = f"Bearer {token}"

    try:
        logger.debug("Supabase session: per-request client created with user JWT")
        yield client
    finally:
        # Nothing to restore — this is a per-request copy that will be GC'd
        pass


def close_supabase_clients() -> None:
    """Cleanup called on application shutdown."""
    global _client, _admin_client
    _client = None
    _admin_client = None
    logger.info("Supabase clients closed")