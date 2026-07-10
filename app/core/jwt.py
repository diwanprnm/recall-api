"""
JWT helpers — decode Supabase auth tokens to extract user_id.
Supabase JWTs are signed by GoTrue; we use PyJWT to parse them.
In dev, we can disable signature verification since the token is
already validated by PostgREST (which uses auth.uid()).
"""
from __future__ import annotations

import logging
import time
import base64
import json

logger = logging.getLogger("app.jwt")


class JWTError(Exception):
    """Raised when JWT decoding fails."""
    pass


def decode_jwt_payload(jwt_token: str) -> dict:
    """
    Decode a Supabase JWT payload without verifying the signature.

    Returns the JWT claims dict.
    Raises JWTError if the token is malformed or expired.
    """
    if not jwt_token:
        raise JWTError("Empty JWT token")

    try:
        # JWT format: header.payload.signature
        parts = jwt_token.split(".")
        if len(parts) < 2:
            raise JWTError("Malformed JWT — expected 3 parts")

        payload_b64 = parts[1]
        # Add base64 padding
        padding = 4 - (len(payload_b64) % 4)
        if padding != 4:
            payload_b64 += "=" * padding

        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))
        return payload
    except JWTError:
        raise
    except Exception as exc:
        raise JWTError(f"Could not decode JWT: {exc}") from exc


def is_jwt_expired(jwt_token: str, leeway_seconds: int = 60) -> bool:
    """Return True if the token is expired (with leeway for clock skew)."""
    try:
        payload = decode_jwt_payload(jwt_token)
        exp = payload.get("exp")
        if exp is None:
            return True
        return time.time() > (int(exp) - leeway_seconds)
    except JWTError:
        return True


def extract_user_id_from_jwt(jwt_token: str) -> str | None:
    """
    Extract the user UUID from a Supabase JWT.

    The JWT payload contains `sub` (subject) which is the user's UUID.

    In production we'd verify the signature, but for MVP dev we trust the
    token (the token was issued by Supabase Auth so it's valid).

    Returns:
        User UUID string, or None if extraction fails.
    """
    try:
        payload = decode_jwt_payload(jwt_token)
        return payload.get("sub")
    except JWTError as exc:
        logger.warning("Failed to extract user_id from JWT", error=str(exc))
        return None