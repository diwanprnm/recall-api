"""
Shared route dependencies — single source of auth truth.

Replaces the duplicated ``_require_auth`` / ``AuthDep`` scattered across
``auth.py``, ``items.py``, ``search.py`` and ``routes/__init__.py``.

Auth is now a custom HS256 JWT (see app.core.security). The token is verified
(signature + exp) and the user UUID is read from the ``sub`` claim.
"""
from __future__ import annotations

import jwt as pyjwt
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.core.security import decode_token


def _require_auth(request: Request) -> str:
    """Extract the Bearer token from request headers (verified by AuthDep)."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header. Expected: 'Bearer <jwt>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return auth


AuthDep = Annotated[str, Depends(_require_auth)]


def get_current_user_id(auth: AuthDep) -> str:
    """
    Verify the JWT and return the authenticated user's UUID (the `sub` claim).

    Raises 401 if the token is expired, invalid, or has no subject.
    """
    token = auth.replace("Bearer ", "", 1)
    try:
        claims = decode_token(token)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except pyjwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token (no subject).",
        )
    return str(user_id)
