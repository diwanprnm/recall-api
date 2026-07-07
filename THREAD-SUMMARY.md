# Recall — Project Handoff Notes

**Last updated:** Monday, July 6, 2026
**User:** theone (diwanprnm on GitHub)
**Conversation thread anchor so this thread is not lost**

---

## 📦 Two GitHub Repos

| Repo | URL | Phase | Stack |
|------|-----|-------|-------|
| **recall-api** | https://github.com/diwanprnm/recall-api | Phase 1 ✅ | FastAPI + Supabase + pgvector + instructor (9router) |
| **recall-web** | https://github.com/diwanprnm/recall-web | Phase 2 ✅ | Next.js 16 (App Router) + TypeScript + Supabase Auth |

---

## 🧠 What's Built (Phase 1 — Backend, complete)

### Architecture
- **AI pipeline:** single GPT-4o-mini call via instructor → summary + classification + entities + tags (`app/services/ai_service.py`)
- **Embeddings:** text-embedding-3-small (1536d) for enriched semantic search
- **Search:** pgvector cosine similarity via Supabase RPC `match_items()`
- **DB:** 4 SQL migrations (extensions, schema+RLS, search functions, seed)

### API endpoints (all need Auth: `Bearer <jwt>`)
```
POST   /api/items              # save + extract + AI analyse + embed
GET    /api/items              # list, filter by platform/tag/favorite
GET    /api/items/{id}
PATCH  /api/items/{id}
DELETE /api/items/{id}         # soft archive
POST   /api/items/{id}/reanalyse
POST   /api/search             # semantic search
GET    /api/search/related/{id}
GET    /api/auth/profile
GET    /health, /health/ready
```

### Files (34 total at /root/recall-api/)
```
app/
├── main.py              # FastAPI factory, lifespan, CORS
├── core/
│   ├── config.py        # Pydantic Settings
│   ├── supabase.py      # anon + service-role clients, JWT session manager
│   ├── ai.py            # instructor + 9router
│   └── logging.py       # structlog
├── schemas/schemas.py   # 25+ Pydantic models
├── services/
│   ├── ai_service.py    # "ONE call to rule them all"
│   ├── embedding_service.py
│   └── extraction_service.py  # Open Graph / Twitter Cards / ld+json
└── routes/
    ├── auth.py
    ├── items.py
    └── search.py

migrations/
├── 001_extensions.sql
├── 002_schemas.sql
├── 003_search_functions.sql
└── 004_seed.sql

tests/test_schemas.py    # 20/20 passing
.env.example
README.md
pyproject.toml
```

### Cost analysis (per docs)
- $0.00005/item (GPT-4o-mini) + $0.00002/item (embedding)
- 10K items/month = ~$0.50/month total

---

## 🎨 What's Built (Phase 2 — Frontend MVP, complete)

### Routes
```
/                          → Auth (login/signup) → redirect /dashboard
/dashboard                 → Library (grid/list view, platform filters)
/dashboard/search          → Semantic search page
```

### Components
- **AuthPage** (`src/components/auth/auth-page.tsx`)
- **Sidebar** + **Header** (`src/components/layout/`)
- **ItemCard** (grid + list views, quality badges, AI tag, platform icons)
- **AddItemModal** (URL input + platform dropdown → POST /api/items)
- **Toast/Toaster** + **Button** + **EmptyState**

### Hooks
- `useAuth` — Supabase session + JWT helper
- `useItems` — items CRUD with optimistic updates
- `useSearch` — semantic search via FastAPI

### Files (29+ at /root/recall-web/)
```
src/
├── app/
│   ├── (app)/dashboard/
│   │   ├── page.tsx           # Library
│   │   └── search/page.tsx    # Semantic search
│   ├── page.tsx               # Auth (root)
│   ├── layout.tsx
│   └── globals.css
├── components/
│   ├── auth/auth-page.tsx
│   ├── items/item-card.tsx
│   ├── layout/{sidebar,header}.tsx
│   └── ui/{button,empty-state,toaster}.tsx
├── hooks/
│   ├── use-auth.ts
│   ├── use-items.ts
│   ├── use-search.ts
│   └── use-toast.ts
├── lib/
│   ├── api/client.ts          # FastAPI client
│   └── supabase/{client,server}.ts
└── types/index.ts             # mirrors FastAPI Pydantic types
```

### Verification
- `tsc --noEmit` ✓ Clean
- `next build` ✓ Built in 20s, all 4 routes prerendered

---

## 🔑 Key Conventions

### Types MUST stay in sync
`recall-web/src/types/index.ts` ⇄ `recall-api/app/schemas/schemas.py`

### Platform-aware
- Backend auto-detects platform from URL
- Frontend shows platform badge + colored icon

### AI Failures Are Graceful
If AI service is down, item still saved with `summary: null`. UI shows badges only when `summary` exists.

---

## 📋 Outstanding (Future Phases)

| Phase | Focus | Status |
|-------|-------|--------|
| 3 | Plasmo browser extension (Twitter/X, Reddit, YouTube) | Pending |
| 4a | Item detail page (currently click → opens URL externally) | Pending |
| 4b | Daily digest email (Resend/SendGrid) | Pending |
| 4c | Knowledge graph view | Skipped (nice-to-have) |
| 4d | Tag/Category management pages | Skipped |

---

## 🔐 Environment Variables

### Backend (recall-api/.env)
```
SUPABASE_URL=https://hynoqplwtzibrqxzrat.supabase.co
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...
OPENAI_API_KEY=          # 9router key
OPENAI_BASE_URL=https://api.9router.com/v1
HOST=0.0.0.0
PORT=8000
ENVIRONMENT=development
```

### Frontend (recall-web/.env.local)
```
NEXT_PUBLIC_SUPABASE_URL=https://hynoqplwtzibrqxzrat.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=...
NEXT_PUBLIC_API_URL=http://localhost:8000
```

---

## 🚀 Run Locally

```bash
# Backend
cd /root/recall-api
source .venv/bin/activate  # Windows: .venv/Scripts/activate
uvicorn app.main:app --reload --port 8000

# Frontend
cd /root/recall-web
npm install
npm run dev   # http://localhost:3000
```

---

## 💬 Thread Context (what theone was doing)

- 4 Jul 2026: Brainstormed "Recall" idea — Read IDEATION-CANVAS, PORTFOLIO-PLAN, TECH-STACK
- 6 Jul 2026 (morning): Built Phase 1 backend → pushed to GitHub
- 6 Jul 2026 (afternoon): Hit `role "supabase_service_role" does not exist` errors on Supabase free tier → patched GRANT statements with DO blocks
- 6 Jul 2026 (evening): Built Phase 2 Next.js frontend → pushed to GitHub
- 6 Jul 2026 (later): **Asked me to save this thread** (this file)
- User prefers: Indonesian, concise responses, real executing not just describing

---

## 📞 If the thread is lost — start here

The user would say something like "lanjut Phase 3" or "ringkas state recall". **Open this file first** to see where we left off, then check both repos for the latest commit on `main`.
