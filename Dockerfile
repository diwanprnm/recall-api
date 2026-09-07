# Build-time arguments for backend
ARG SUPABASE_URL
ARG SUPABASE_SERVICE_ROLE_KEY
ARG OPENAI_API_KEY

FROM python:3.11-slim

# Pass build args to env
ENV SUPABASE_URL=${SUPABASE_URL} \
    SUPABASE_SERVICE_ROLE_KEY=${SUPABASE_SERVICE_ROLE_KEY} \
    OPENAI_API_KEY=${OPENAI_API_KEY}

WORKDIR /app

# Install system dependencies dan buat user 'recall'
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libffi8 \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r recall && useradd -r recall -g recall

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=development

# 1. Install dependencies dulu (layer cache stabil — hanya berubah kalau pyproject.toml berubah)
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# 2. Copy source code setelah install (tidak merusak cache layer pip)
COPY --chown=recall:recall ./app ./app

# Switch to non-root user demi keamanan
USER recall

EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# Command Uvicorn sekarang pasti bisa menemukan folder 'app'
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--log-level", "info"]
