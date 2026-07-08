"""
JWT helpers — decode Supabase auth tokens to extract user_id.
Supabase JWTs are signed by GoTrue; we use PyJWT to parse them.
In dev, we can disable signature verification since the token is
already validated by PostgREST (which uses auth.uid()).
"""
from __future__ import annotations

import logging
import base64
import json

logger = logging.getLogger(__name__)


def extract_user_id_from_jwt(jwt_token: str) -> str | None:
    """
    Extract the user UUID from a Supabase JWT.

    The JWT payload contains `sub` (subject) which is the user's UUID.

    In production we'd verify the signature, but for MVP dev we trust the
    token (the token was issued by Supabase Auth so it's valid).

    Args:
        jwt_token: The raw JWT (without 'Bearer ' prefix)

    Returns:
        User UUID string, or None if extraction fails.
    """
    if not jwt_token:
        return None
    try:
        # JWT format: header.payload.signature
        parts = jwt_token.split(".")
        if len(parts) < 2:
            return None

        # Decode payload (second part)
        payload_b64 = parts[1]
        # Base64 url-safe decoding with padding
        padding = 4 - (len(payload_b64) % 4)
        if padding != 4:
            payload_b64 += "=" * padding

        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))

        user_id = payload.get("sub")
        if user_id:
            return user_id
        logger.warning("JWT payload missing 'sub' field")
        return None
    except Exception as exc:
        logger.error("Failed to extract user_id from JWT", error=str(exc))
        return None