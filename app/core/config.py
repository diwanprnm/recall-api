"""
Application settings — loaded from environment variables.

Uses Pydantic Settings v2 with dotenv support.
All values are validated at startup (fails fast on misconfiguration).
"""
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Central configuration. All sensitive values come from environment,
    never hardcoded. In production these are injected by the VPS shell env.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,       # SUPABASE_URL == supabase_url
        extra="ignore",             # ignore unknown env vars silently
        revalidate_classes=True,
    )

    # ── Supabase ──────────────────────────────────────────────────────────────
    supabase_url: str = Field(..., description="Supabase project URL")
    supabase_anon_key: str = Field(..., description="Supabase anon (client-side) key")
    supabase_service_role_key: str = Field(
        ..., description="Supabase service role key (server-side only!)"
    )

    # ── AI / 9router ──────────────────────────────────────────────────────────
    openai_api_key: str = Field(..., description="9router API key")
    openai_base_url: str = Field(
        "https://api.9router.com/v1",
        description="OpenAI-compatible base URL for 9router",
    )
    ai_model: str = Field("gpt-4o-mini", description="Chat model for AI pipeline")
    embedding_model: str = Field(
        "text-embedding-3-small", description="Embedding model for semantic search"
    )
    embedding_dimensions: int = Field(
        1536, description="Embedding vector dimensions (1536 for text-embedding-3-small)"
    )

    # ── Server ────────────────────────────────────────────────────────────────
    host: str = Field("0.0.0.0")
    port: int = Field(8000)
    environment: str = Field("development")
    debug: bool = Field(False)

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        allowed = {"development", "staging", "production"}
        if v not in allowed:
            raise ValueError(f"ENVIRONMENT must be one of {allowed}, got: {v!r}")
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    # ── CORS ──────────────────────────────────────────────────────────────────
    allowed_origins: str = Field(
        "http://localhost:3000", description="Comma-separated list of allowed CORS origins"
    )

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    # ── Sentry (optional) ─────────────────────────────────────────────────────
    sentry_dsn: str | None = Field(None)

    # ── Resend / Email (optional) ─────────────────────────────────────────────
    resend_api_key: str | None = Field(None)
    digest_email_from: str | None = Field(None)


# ── Global singleton (lazily initialised on first access) ─────────────────────
_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
