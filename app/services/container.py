"""
Service container — holds the AI / embedding service singletons.

Lives in ``app.services`` so routes depend on this (one-way:
routes → services → core) instead of importing ``app.main``.
"""
from __future__ import annotations

from app.services.ai_service import AIService
from app.services.embedding_service import EmbeddingService

_ai_service: AIService | None = None
_embedding_service: EmbeddingService | None = None


def init_services() -> None:
    """Initialise services. Called once during app lifespan startup."""
    global _ai_service, _embedding_service
    _embedding_service = EmbeddingService()
    _ai_service = AIService(_embedding_service)


def get_ai_service() -> AIService:
    if _ai_service is None:
        raise RuntimeError("Application not started — call init_services() first")
    return _ai_service


def get_embedding_service() -> EmbeddingService:
    if _embedding_service is None:
        raise RuntimeError("Application not started — call init_services() first")
    return _embedding_service
