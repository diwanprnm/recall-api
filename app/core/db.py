"""
Database layer — raw Postgres via psycopg (replaces the Supabase/PostgREST client).

Why psycopg (sync) inside async handlers?
- The supabase-py client was sync and ran in a thread pool anyway
  (`asyncio.to_thread`). psycopg's sync Connection does the same job directly.
- One connection per uvicorn worker, reused across requests. All SQL runs in
  `asyncio.to_thread` so the event loop stays free.

Row isolation is now APP-ENFORCED: every query must include `user_id = %s`.
There is no RLS — the route handler supplies the authenticated user_id
(extracted from the JWT by app.routes.deps).
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
import structlog
from psycopg.rows import dict_row

from app.core.config import get_settings

logger = structlog.get_logger(__name__)

_conn: psycopg.Connection | None = None


def _get_conn() -> psycopg.Connection:
    """Lazily open (and reuse) a single sync connection for this worker."""
    global _conn
    if _conn is None or _conn.closed:
        cfg = get_settings()
        _conn = psycopg.connect(
            cfg.database_url,
            row_factory=dict_row,
            # Each db_query/db_write is its own transaction. Without this a failed
            # statement aborts the shared connection's txn and every later query
            # fails with InFailedSqlTransaction until restart.
            autocommit=True,
        )
        logger.info("Postgres connection opened", url=cfg.database_url.split("@")[-1])
    return _conn


def get_conn() -> psycopg.Connection:
    """Public accessor for the worker connection (used at startup)."""
    return _get_conn()


async def close_db() -> None:
    """Close the worker connection (called on shutdown)."""
    global _conn
    if _conn is not None and not _conn.closed:
        _conn.close()
    _conn = None


@contextmanager
def transaction():
    """Transaction context manager: commit on success, rollback on error."""
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


async def db_query(
    sql: str,
    params: Sequence[Any] = (),
    *,
    fetch: bool = True,
) -> list[dict]:
    """
    Run a SQL statement in a thread pool, returning rows as dicts.

    For writes you usually pass fetch=False (returns []). For reads it returns
    a list of dict rows. Use within a `transaction()` block for multi-statement
    atomicity, otherwise each call auto-commits.
    """
    conn = _get_conn()

    def _run() -> list[dict]:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            if fetch:
                return cur.fetchall() or []
            return []

    return await asyncio.to_thread(_run)


async def db_write(sql: str, params: Sequence[Any] = ()) -> None:
    """Run a write (INSERT/UPDATE/DELETE) and commit."""
    conn = _get_conn()

    def _run() -> None:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
        conn.commit()

    await asyncio.to_thread(_run)
