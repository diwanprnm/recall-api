# ── Recall API Dockerfile ─────────────────────────────────────
# FastAPI + instructor + Supabase
# Multi-stage build: 
#   1. builder: install deps in a temporary image
#   2. runtime: copy code + deps into smaller runtime image

FROM python:3.11-slim AS builder

WORKDIR /app

# Install system deps (uvloop, cryptography need C extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency files first for better caching
COPY pyproject.toml ./

# Install Python deps
RUN pip install --no-cache-dir --upgrade pip wheel && \
    pip wheel --no-cache-dir --wheel-dir=/wheels .

# ── Runtime stage ──────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Install only the runtime system libs we need
RUN apt-get update && apt-get install -y --no-install-recommends \
    libffi8 \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r recall && useradd -r recall -g recall

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY --from=builder /wheels /wheels
COPY pyproject.toml ./
RUN pip install --no-cache-dir --no-index --find-links=/wheels . && \
    rm -rf /wheels

# Copy source
COPY app/ ./app/

# Switch to non-root
USER recall

EXPOSE 8000

# Healthcheck for container orchestration
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--log-level", "info"]