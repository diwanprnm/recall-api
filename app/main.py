"""
FastAPI application factory and lifecycle management.

Architecture:
  • App lifecycle (startup/shutdown) initialises the DB connection and AI services
  • Services are exposed via get_ai_service() / get_embedding_service() singletons
  • CORS configured per environment
  • Sentry integrated for production error tracking
  • OpenAPI docs at /docs (development only)
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.db import close_db, get_conn
from app.core.logging import configure_logging
from app.routes import auth, categories, digest, items, search, tags
from app.services import container

logger = structlog.get_logger()


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

    # ── Initialise DB connection ──────────────────────────────────────────────
    try:
        get_conn()
        logger.info("Database connection ready", url=cfg.database_url)
    except Exception as exc:
        logger.error("Failed to init database connection", error=str(exc))
        raise

    # ── Initialise AI services ────────────────────────────────────────────────
    try:
        container.init_services()
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
    await close_db()
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

    # ── Swagger "Authorize" button: lets you paste a Bearer JWT once,
    #    applied to every locked endpoint in /docs. ─────────────────────────────
    app.swagger_ui_init_oauth = None

    from fastapi.openapi.utils import get_openapi

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
        for path in schema["paths"].values():
            for op in path.values():
                if isinstance(op, dict):
                    op.setdefault("security", [{"BearerAuth": []}])
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi

    # ── Middleware ──────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.allowed_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Range", "X-Total-Count"],
    )

    # ── Routes ──────────────────────────────────────────────────────────────────
    app.include_router(auth.router, prefix="/api")
    app.include_router(items.router, prefix="/api")
    app.include_router(search.router, prefix="/api")
    app.include_router(tags.router, prefix="/api")
    app.include_router(categories.router, prefix="/api")
    app.include_router(digest.router, prefix="/api")

    # ── Health check ────────────────────────────────────────────────────────────
    @app.get("/health", tags=["health"])
    async def health_check():
        return {"status": "healthy", "service": "recall-api"}

    @app.get("/health/ready", tags=["health"])
    async def readiness_check():
        """Full readiness: checks database connectivity."""
        try:
            from app.core.db import db_query
            await db_query("SELECT 1")
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
        logger.warning(
            "Request validation error",
            path=request.url.path,
            errors=exc.errors(),
        )
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Validation error",
                "errors": exc.errors(),
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.error(
            "Unhandled exception",
            path=request.url.path,
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error. This has been logged."},
        )

    # ── Catch response serialization errors (Pydantic ValidationError) ─────
    # These happen when DB returns data that doesn't match the response model,
    # and they are NOT caught by the generic Exception handler above.
    from pydantic import ValidationError

    @app.exception_handler(ValidationError)
    async def pydantic_validation_error_handler(request: Request, exc: ValidationError):
        logger.error(
            "Response serialization error — DB returned invalid data",
            path=request.url.path,
            method=request.method,
            errors=exc.errors(),
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error: response data is invalid."},
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
