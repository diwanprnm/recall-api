FROM python:3.11-slim

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
    PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml README.md ./

# PERBAIKAN 1 & 2: Gunakan user 'recall' dan sesuaikan struktur folder
COPY --chown=recall:recall ./app ./app

# Jalankan instalasi setelah semua file siap
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# Switch to non-root user demi keamanan
USER recall

EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# Command Uvicorn sekarang pasti bisa menemukan folder 'app'
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--log-level", "info"]
