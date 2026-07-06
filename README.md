# Recall API — AI-Powered Social Media Knowledge Manager

**Backend API for Recall** — a portfolio-grade knowledge management app that saves, categorises, and surfaces content from Twitter/X, Reddit, YouTube, Instagram, LinkedIn, and more.

Built with **FastAPI + instructor + 9router** (OpenAI-compatible), backed by **Supabase** (PostgreSQL + pgvector).

> *Your Second Brain for Social Media* — save with one click, search with ideas, not keywords.

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         FRONTEND (Next.js / Plasmo)                       │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │  HTTPS + JWT Bearer Token
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                      RECALL API (FastAPI) — :8000                        │
│                                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────────┐  │
│  │  /api/items  │  │ /api/search   │  │  /api/auth                  │  │
│  │  (CRUD)      │  │ (semantic)    │  │  (Supabase JWT verify)      │  │
│  └──────┬───────┘  └──────┬───────┘  └────────────────────────────┘  │
│         │                 │                                             │
│         └────────┬────────┘                                             │
│                  ▼                                                       │
│         ┌────────────────┐                                             │
│         │  AI PIPELINE   │  ← instructor (9router)                     │
│         │  ONE call does │     GPT-4o-mini → structured output          │
│         │  summary + tags │     text-embedding-3-small → vectors         │
│         │  + entities    │                                             │
│         └────────┬───────┘                                             │
└──────────────────┼──────────────────────────────────────────────────────┘
                   │  Service Role Key (server-side only)
                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                          SUPABASE (Free Tier)                            │
│                                                                          │
│  ┌─────────────┐  ┌──────────────────┐  ┌─────────────────────────────┐ │
│  │ PostgreSQL  │  │ pgvector (1536d) │  │ Auth (JWT + OAuth)          │ │
│  │   RLS       │  │ Semantic Search  │  │ Email / Magic Link          │ │
│  └─────────────┘  └──────────────────┘  └─────────────────────────────┘ │
│                                                                          │
│  REST API auto-generated from schema                                     │
└──────────────────────────────────────────────────────────────────────────┘
```

### AI Pipeline (Per IDEATION-CANVAS: One Call to Rule Them All)

Every saved item runs through a **single GPT-4o-mini call** that returns:

| Field | Description |
|-------|-------------|
| `summary.one_liner` | 1-2 sentence summary |
| `summary.key_points` | 3-5 key takeaways |
| `classification.primary_topics` | Top 3 topics |
| `classification.sentiment` | positive/negative/neutral/mixed |
| `classification.relevance_score` | 1-5 quality rating |
| `entities` | people, orgs, products, technologies, hashtags |
| `suggested_tags` | 5-10 auto-generated tags |
| `quality_score` | 1-5 content quality |

Then: `text-embedding-3-small` (1536 dims) → **pgvector** for semantic search.

### Cost Breakdown (Per PORTFOLIO-PLAN)

| Task | Cost |
|------|------|
| 1 item AI analysis (GPT-4o-mini) | ~$0.00005 |
| 1 item embedding | ~$0.00001 |
| 10K items/month (analysis) | ~$0.50 |
| **Total AI/month (10K items)** | **~$0.50** |

---

## 📁 Project Structure

```
recall-api/
├── app/
│   ├── main.py              # FastAPI app factory + lifespan
│   ├── core/
│   │   ├── config.py        # Settings from env (Pydantic)
│   │   ├── supabase.py      # Supabase async/sync clients
│   │   ├── ai.py            # Instructor + 9router client
│   │   └── logging.py       # structlog configuration
│   ├── schemas/
│   │   └── schemas.py       # All Pydantic request/response models
│   ├── services/
│   │   ├── ai_service.py       # AI pipeline (the ONE call)
│   │   ├── embedding_service.py # text-embedding-3-small
│   │   └── extraction_service.py # Open Graph / HTML parsing
│   └── routes/
│       ├── auth.py          # Supabase JWT verification
│       ├── items.py         # CRUD + AI processing
│       └── search.py        # pgvector semantic search
├── migrations/
│   ├── 001_extensions.sql   # pgvector, pgcrypto, unaccent
│   ├── 002_schemas.sql      # Tables + RLS policies
│   ├── 003_search_functions.sql # RPC: match_items(), hybrid_search()
│   └── 004_seed.sql         # Dev seed data
├── tests/                   # pytest + Pydantic unit tests
├── pyproject.toml           # Dependencies + tools config
└── .env.example             # Env var template
```

---

## 🚀 Getting Started

### Prerequisites

- Python 3.11+
- A **Supabase** project (free tier: 500MB PostgreSQL)
- A **9router** API key (OpenAI-compatible — you already have one)

### 1. Clone / Navigate

```bash
cd /root/recall-api
```

### 2. Setup Python Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Configure Environment

```bash
cp .env.example .env
# Edit .env with your credentials:
#   SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY
#   OPENAI_API_KEY, OPENAI_BASE_URL
```

### 4. Run Database Migrations (Supabase SQL Editor)

Go to **Supabase → SQL Editor** and run each migration in order:

```sql
-- 1. Enable extensions
-- Copy-paste: migrations/001_extensions.sql

-- 2. Create schemas + RLS
-- Copy-paste: migrations/002_schemas.sql

-- 3. Create search functions
-- Copy-paste: migrations/003_search_functions.sql
```

### 5. Start the Server

```bash
uvicorn app.main:app --reload --port 8000
```

- **API**: http://localhost:8000
- **Docs**: http://localhost:8000/docs (Swagger UI)
- **ReDoc**: http://localhost:8000/redoc

### 6. Run Tests

```bash
pytest tests/ -v
```

---

## 🔌 API Reference

All endpoints require `Authorization: Bearer <supabase_jwt>` header.

### Items

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/items` | Save new content (extract → AI analyse → embed → store) |
| `GET` | `/api/items` | List items (filter by platform, tag, category, favorite) |
| `GET` | `/api/items/{id}` | Get single item |
| `PATCH` | `/api/items/{id}` | Update item (partial) |
| `DELETE` | `/api/items/{id}` | Archive item (soft delete) |
| `POST` | `/api/items/{id}/reanalyse` | Re-run AI on existing item |

### Search

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/search` | Semantic search (vector similarity) |
| `GET` | `/api/search/related/{id}` | Find items similar to this one |

### Auth

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/auth/profile` | Get current user profile |
| `POST` | `/api/auth/verify` | Verify JWT is valid |

### Example: Save an item

```bash
curl -X POST http://localhost:8000/api/items \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://x.com/example/status/123",
    "platform": "twitter",
    "title": "Thread: Building AI Products"
  }'
```

### Example: Semantic search

```bash
curl -X POST http://localhost:8000/api/search \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "things I saved about LLM fine-tuning",
    "platform": "twitter",
    "limit": 10
  }'
```

---

## 🔑 Key Design Decisions

### Why pgvector over Qdrant?
Supabase free tier includes pgvector. It's already there — no separate service to manage. The `match_items()` RPC function abstracts the SQL away from the app code.

### Why instructor for AI?
Instructor provides **structured output** (Pydantic models from LLM) with automatic retry on malformed JSON. One model → no parsing errors. This is the key to the "one call" architecture.

### Why RLS over middleware auth?
Row-Level Security means Supabase enforces data isolation at the database level — even if a bug leaks the service role key, the database itself prevents cross-user access. Defense in depth.

### Why `match_items` as RPC instead of raw SQL?
RPC keeps SQL migration in the DB layer, not the app layer. The app only calls `sb.rpc("match_items", {...})`. Easy to evolve the search algorithm without changing Python code.

---

## 📊 Phase 1 Deliverables (Days 1-3) ✅

- [x] Project structure (`/root/recall-api`)
- [x] Supabase schema + pgvector + RLS (4 migration files)
- [x] FastAPI backend with config, Supabase client, AI client
- [x] AI pipeline: one-call analysis (summary + classification + entities + tags)
- [x] Embedding pipeline: enriched text embedding for semantic search
- [x] Content extraction: Open Graph, Twitter Cards, HTML meta, ld+json
- [x] API routes: items CRUD + semantic search + auth verification
- [x] Error handling + input validation + production-ready patterns
- [x] Unit tests for schemas

## 📅 Next Steps

| Phase | Timeline | Focus |
|-------|----------|-------|
| **Phase 2** | Days 4-7 | Next.js frontend (Dashboard, Library, Search UI) |
| **Phase 3** | Days 8-11 | Plasmo browser extension (Twitter, Reddit, YouTube) |
| **Phase 4** | Days 12-14 | Daily digest email, polish, portfolio docs |

---

## 📝 Environment Variables Reference

```env
# Supabase (from Settings → API)
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=eyJ...
SUPABASE_SERVICE_ROLE_KEY=eyJ...

# 9router (your existing OpenAI-compatible key)
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.9router.com/v1

# Model config
AI_MODEL=gpt-4o-mini
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536

# Server
HOST=0.0.0.0
PORT=8000
ENVIRONMENT=development
ALLOWED_ORIGINS=http://localhost:3000

# Optional
# SENTRY_DSN=...
# RESEND_API_KEY=...
# DIGEST_EMAIL_FROM=noreply@recall.app
```