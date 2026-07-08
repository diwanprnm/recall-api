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


@contextmanager
def supabase_session(jwt: str) -> Generator[Client, None, None]:
    """
    Sync context manager: forward the user's JWT so PostgREST can extract
    `auth.uid()` for RLS policy evaluation.

    CRITICAL: ``client.auth.set_session()`` only mutates the *auth* client's
    internal session object. PostgREST REST calls read **HTTP_Authorization**
    directly from the request header (set via the underlying httpx client).

    Therefore we must mutate ``client.options.headers["Authorization"]`` to
    the user's JWT so that every subsequent ``.execute()`` call sends the Bearer
    token. On exit we restore the anon-key default.

    Usage:
        with supabase_session(request.headers.get("Authorization", "")) as sb:
            items = sb.table("items").select("*").execute()
    """
    _init_client()
    client = _client  # type: ignore[union-attr]

    token = jwt.replace("Bearer ", "", 1) if jwt.startswith("Bearer ") else jwt
    if not token:
        logger.warning("No JWT token provided to supabase_session — RLS will use anon key")
        yield client
        return

    # Store the original anon-key default
    previous_auth = client.options.headers.get("Authorization")

    # Override with user JWT so PostgREST sees a real auth.uid()
    client.options.headers["Authorization"] = f"Bearer {token}"

    try:
        logger.debug("Supabase session: using JWT for PostgREST auth")
        yield client
    finally:
        # Restore the anon-key default so other code paths (health checks, etc.)
        # don't leak one user's token into another request
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