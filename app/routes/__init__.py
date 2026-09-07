# app/routes/__init__.py
"""API route modules — each handles a resource area."""
from app.routes.deps import AuthDep  # re-export single auth source

__all__ = ["AuthDep"]
