"""
Auth helpers — JWT issuance/verification + password hashing.

Replaces Supabase GoTrue. We issue our own HS256 JWTs (signed with
JWT_SECRET); the `sub` claim carries the user UUID. Signature is now VERIFIED
(previously skipped, because PostgREST/RLS did the trust boundary — but we own
the secret now, so verification is mandatory).
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone

import jwt as pyjwt
from passlib.context import CryptContext

from app.core.config import get_settings

logger = __import__("logging").getLogger("app.security")

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Password hashing ──────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    # bcrypt has a 72-byte limit; truncate (the schema caps at 128 chars anyway).
    return _pwd.hash(password[:72])


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _pwd.verify(password, hashed)
    except Exception:
        return False


# ── JWT ───────────────────────────────────────────────────────────────────────

def create_access_token(user_id: str) -> str:
    cfg = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + cfg.access_token_expire_minutes * 60,
    }
    return pyjwt.encode(payload, cfg.jwt_secret, algorithm=cfg.jwt_algorithm)


def decode_token(token: str) -> dict:
    """Verify signature + exp, return claims. Raises pyjwt exceptions on failure."""
    cfg = get_settings()
    return pyjwt.decode(token, cfg.jwt_secret, algorithms=[cfg.jwt_algorithm])


def get_user_id_from_token(token: str) -> str | None:
    """Return the `sub` claim, or None if the token is invalid/expired."""
    try:
        return decode_token(token).get("sub")
    except Exception as exc:
        logger.warning("JWT decode failed", error=str(exc))
        return None


# ── Legacy helpers (kept for minimal compat; signature now verified) ──────────

class JWTError(Exception):
    pass


def decode_jwt_payload(jwt_token: str) -> dict:
    return decode_token(jwt_token)


def is_jwt_expired(jwt_token: str, leeway_seconds: int = 60) -> bool:
    try:
        decode_token(jwt_token)
        return False
    except pyjwt.ExpiredSignatureError:
        return True
    except Exception:
        return True


def extract_user_id_from_jwt(jwt_token: str) -> str | None:
    return get_user_id_from_token(jwt_token)
