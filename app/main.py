"""
FastAPI application factory and lifecycle management.

Architecture:
  • App lifecycle (startup/shutdown) initialises Supabase clients and AI services
  • Services are exposed via get_ai_service() / get_embedding_service() singletons
  • CORS configured per environment
  • Sentry integrated for production error tracking
  • OpenAPI docs at /docs (development only)
"""
from __future__ import annotations

import structlog
import uvicorn
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.supabase import close_supabase_clients, get_supabase_client
from app.routes import auth, items, search

logger = structlog.get_logger()

# ── Global service singletons (initialised on startup) ────────────────────────

_ai_service: "app.services.ai_service.AIService | None" = None
_embedding_service: "app.services.embedding_service.EmbeddingService | None" = None


def get_ai_service():
    global _ai_service
    if _ai_service is None:
        raise RuntimeError("Application not started — call lifespan event first")
    return _ai_service


def get_embedding_service():
    global _embedding_service
    if _embedding_service is None:
        raise RuntimeError("Application not started — call lifespan event first")
    return _embedding_service


# ── Lifespan: startup / shutdown ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — initialise on startup, cleanup on shutdown."""
    configure_logging()
    cfg = get_settings()

    logger.info(
        "Starting Recall API",
        version=__import__("app").__version__,
        environment=cfg.environment,
        debug=cfg.debug,
    )

    # ── Initialise Supabase client ────────────────────────────────────────────
    try:
        get_supabase_client()
        logger.info("Supabase client ready", url=cfg.supabase_url)
    except Exception as exc:
        logger.error("Failed to init Supabase client", error=str(exc))
        raise

    # ── Initialise AI services ────────────────────────────────────────────────
    from app.core.ai import get_async_instructor
    from app.services.embedding_service import EmbeddingService
    from app.services.ai_service import AIService

    global _ai_service, _embedding_service
    try:
        instructor_client = get_async_instructor()
        _embedding_service = EmbeddingService(instructor_client)
        _ai_service = AIService(instructor_client, _embedding_service)
        logger.info(
            "AI services initialised",
            model=cfg.ai_model,
            embedding_model=cfg.embedding_model,
        )
    except Exception as exc:
        logger.error("Failed to init AI services", error=str(exc))
        raise

    # ── Sentry (production only) ────────────────────────────────────────────────
    if cfg.sentry_dsn and cfg.is_production:
        import sentry_sdk
        sentry_sdk.init(
            dsn=cfg.sentry_dsn,
            environment=cfg.environment,
            traces_sample_rate=0.1,
        )
        logger.info("Sentry error tracking enabled")

    logger.info("Recall API startup complete", port=cfg.port)

    yield  # ── Application runs here ────────────────────────────────────────────

    # ── Shutdown ────────────────────────────────────────────────────────────────
    logger.info("Shutting down Recall API")
    await close_supabase_clients()
    logger.info("Shutdown complete")


# ── Application factory ────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    cfg = get_settings()

    app = FastAPI(
        title="Recall API",
        description="""
**Recall** — Your Second Brain for Social Media.

AI-powered knowledge manager that:
- Saves content from Twitter/X, Reddit, YouTube, Instagram, LinkedIn
- Auto-classifies, summarises, and tags with GPT-4o-mini
- Enables semantic (vector) search across your entire knowledge base
- Supports daily digest resurfacing

Auth: All endpoints require a Supabase JWT in the `Authorization: Bearer <token>` header.
""",
        version=__import__("app").__version__,
        lifespan=lifespan,
        docs_url="/docs" if not cfg.is_production else None,
        redoc_url="/redoc" if not cfg.is_production else None,
    )

    # ── Middleware ──────────────────────────────────────────────────────────────
   # ── Middleware ──────────────────────────────────────────────────────────────
    
    # Ambil raw string dari konfigurasi
    # raw_origins = getattr(cfg, "allowed_origins", "https://recall.theonezone.my.id")
    
    # # Pecah string berdasarkan koma menjadi List
    # cors_origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]

    # # Fallback aman
    # if not cors_origins:
    #     cors_origins = ["*"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[*],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Range", "X-Total-Count"],
    )

    # ── Routes ──────────────────────────────────────────────────────────────────
    app.include_router(auth.router, prefix="/api")
    app.include_router(items.router, prefix="/api")
    app.include_router(search.router, prefix="/api")

    # ── Health check ────────────────────────────────────────────────────────────
    @app.get("/health", tags=["health"])
    async def health_check():
        return {"status": "healthy", "service": "recall-api"}

    @app.get("/health/ready", tags=["health"])
    async def readiness_check():
        """Full readiness: checks Supabase connectivity using sync admin client."""
        try:
            from app.core.supabase import get_supabase_admin
            admin = get_supabase_admin()
            admin.table("tags").select("id").limit(1).execute()
            return {"status": "ready", "database": "connected"}
        except Exception as exc:
            logger.error("Readiness check failed", error=str(exc))
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not ready",
                    "database": "disconnected",
                    "error": str(exc),
                },
            )

    # ── Global exception handlers ──────────────────────────────────────────────
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Validation error",
                "errors": exc.errors(),
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.error("Unhandled exception", path=request.url.path, error=str(exc), exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error. This has been logged."},
        )

    return app


# ── Application instance ───────────────────────────────────────────────────────

app = create_app()


if __name__ == "__main__":
    cfg = get_settings()
    uvicorn.run(
        "app.main:app",
        host=cfg.host,
        port=cfg.port,
        reload=cfg.debug,
        workers=1 if cfg.debug else 4,
        log_level="debug" if cfg.debug else "info",
    )