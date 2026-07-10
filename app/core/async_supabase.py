"""
Async Supabase wrapper — runs sync Supabase calls in a thread pool
so they don't block the FastAPI event loop.

The supabase-py library only provides a sync client. Inside async route
handlers, calling .execute() directly blocks the event loop for the
duration of the HTTP round-trip to PostgREST (often 50-200ms).

This module wraps every sync operation with asyncio.to_thread() so the
event loop stays free to serve other requests.
"""
from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager

import structlog
from supabase import Client

from app.core.supabase import supabase_session

logger = structlog.get_logger(__name__)


class AsyncSupabaseSession:
    """
    Async context manager that wraps a sync Supabase client.

    Usage:
        async with async_supabase_session(auth) as sb:
            # sb is a sync client, but all .execute() calls run in thread pool
            resp = await sb.table("items").select("*").execute()
    """

    def __init__(self, jwt: str):
        self._jwt = jwt
        self._session_ctx: Generator[Client, None, None] | None = None
        self._client: Client | None = None

    async def __aenter__(self) -> Client:
        # Create the sync session context
        self._session_ctx = supabase_session(self._jwt)
        self._client = self._session_ctx.__enter__()
        return self._client

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session_ctx:
            self._session_ctx.__exit__(exc_type, exc_val, exc_tb)
        return False


async def execute_async(query) -> object:
    """
    Run a Supabase sync query builder's .execute() in a thread pool.

    This is the core fix: every .execute() call goes through here so the
    event loop is never blocked by synchronous HTTP I/O.

    Usage:
        resp = await execute_async(sb.table("items").select("*"))
        resp = await execute_async(sb.table("items").insert(data))
    """
    return await asyncio.to_thread(query.execute)


async def rpc_async(sb: Client, function_name: str, params: dict) -> object:
    """
    Run a Supabase RPC call asynchronously via thread pool.

    Usage:
        result = await rpc_async(sb, "match_items", {"query_embedding": vec})
    """
    return await asyncio.to_thread(sb.rpc(function_name, params).execute)
