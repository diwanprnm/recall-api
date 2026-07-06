-- ============================================================================
-- Migration: 002_schemas.sql
-- Description: Core database schema — Users, Items, Tags, Categories
-- RLS enabled on all tables for security
-- Run AFTER 001_extensions.sql
-- ============================================================================

-- ── Users (extends Supabase Auth) ─────────────────────────────────────────────

-- We use Supabase Auth for auth. The `users` table here stores extra profile
-- data that Supabase Auth doesn't handle (e.g., digest settings).
-- Supabase Auth creates a `auth.users` table automatically.
CREATE TABLE IF NOT EXISTS public.users (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    email       TEXT        NOT NULL UNIQUE,
    name        TEXT,
    avatar_url  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Digest Settings ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.digest_settings (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    enabled     BOOLEAN     NOT NULL DEFAULT true,
    frequency   TEXT        NOT NULL DEFAULT 'weekly'
                    CHECK (frequency IN ('daily', 'weekly', 'biweekly')),
    last_sent_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT digest_settings_user_id_unique UNIQUE (user_id)
);

-- ── Categories ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.categories (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    name        TEXT        NOT NULL,
    color       TEXT        NOT NULL DEFAULT '#10b981',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Each user has unique category names
    CONSTRAINT categories_user_name_unique UNIQUE (user_id, name)
);

-- ── Tags ──────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.tags (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    name        TEXT        NOT NULL,
    color       TEXT        NOT NULL DEFAULT '#6366f1',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Tag names are unique per user (case-insensitive)
    CONSTRAINT tags_user_name_unique UNIQUE (user_id, name)
);

-- ── Items (the core table) ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.items (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID        NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,

    -- ── Platform info ──────────────────────────────────────────────────────
    platform        TEXT        NOT NULL,  -- twitter, instagram, youtube, reddit, linkedin, tiktok, facebook, web
    url             TEXT        NOT NULL,
    original_id     TEXT,                 -- ID on original platform

    -- ── Content (extracted from URL or provided directly) ────────────────────
    title           TEXT,
    text            TEXT,                 -- Full extracted text (up to 50K chars)
    author          TEXT,                 -- Display name
    author_handle   TEXT,                 -- @username
    author_avatar   TEXT,                 -- Profile image URL
    thumbnail_url   TEXT,                 -- Content thumbnail

    -- ── AI-generated enrichment ──────────────────────────────────────────────
    summary         TEXT,                 -- One-liner summary
    key_points      JSONB     DEFAULT '[]',  -- [{"point": "..."}]
    sentiment       TEXT,                 -- positive, negative, neutral, mixed
    quality_score   INTEGER,              -- 1-5

    -- ── pgvector embedding (1536-dim text-embedding-3-small) ────────────────
    embedding       VECTOR(1536),

    -- ── Full AI analysis JSON (structured output from instructor) ────────────
    analysis_json   JSONB,

    -- ── Metadata ────────────────────────────────────────────────────────────
    saved_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at         TIMESTAMPTZ,
    is_favorite     BOOLEAN     NOT NULL DEFAULT false,
    is_archived     BOOLEAN     NOT NULL DEFAULT false,

    -- ── Category ─────────────────────────────────────────────────────────────
    category_id     UUID        REFERENCES public.categories(id) ON DELETE SET NULL,

    -- ── Constraints ──────────────────────────────────────────────────────────
    CONSTRAINT items_user_url_unique UNIQUE (user_id, url)
);

-- ── Item Tags (many-to-many join) ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.item_tags (
    item_id     UUID    NOT NULL REFERENCES public.items(id) ON DELETE CASCADE,
    tag_id      UUID    NOT NULL REFERENCES public.tags(id) ON DELETE CASCADE,

    PRIMARY KEY (item_id, tag_id)
);

-- ── Indexes ────────────────────────────────────────────────────────────────────

-- Performance indexes for common queries
CREATE INDEX IF NOT EXISTS idx_items_user_saved
    ON public.items (user_id, saved_at DESC);

CREATE INDEX IF NOT EXISTS idx_items_user_platform
    ON public.items (user_id, platform);

CREATE INDEX IF NOT EXISTS idx_items_user_category
    ON public.items (user_id, category_id)
    WHERE category_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_items_user_favorite
    ON public.items (user_id, is_favorite)
    WHERE is_favorite = true;

-- pgvector index for semantic search — IVFFlat is faster for large datasets
-- The list count (100) is a reasonable default for <1M rows
CREATE INDEX IF NOT EXISTS idx_items_embedding_ivfflat
    ON public.items
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- JSONB index for AI analysis queries
CREATE INDEX IF NOT EXISTS idx_items_analysis_jsonb
    ON public.items USING gin (analysis_json);

-- Text search index (hybrid: vector + keyword)
CREATE INDEX IF NOT EXISTS idx_items_text_search
    ON public.items USING gin (
        to_tsvector('english', coalesce(title, '') || ' ' || coalesce(text, ''))
    );

-- Tag join indexes
CREATE INDEX IF NOT EXISTS idx_item_tags_tag_id ON public.item_tags (tag_id);
CREATE INDEX IF NOT EXISTS idx_item_tags_item_id ON public.item_tags (item_id);

-- ── Row Level Security (RLS) ─────────────────────────────────────────────────

-- Enable RLS on all tables
ALTER TABLE public.users         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.digest_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.items         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tags          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.categories    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.item_tags     ENABLE ROW LEVEL SECURITY;

-- Helper: get current user ID from Supabase auth.jwt()
CREATE OR REPLACE FUNCTION public.current_user_id()
RETURNS UUID
STABLE
LANGUAGE sql
SECURITY DEFINER
AS $$
  SELECT NULLIF(nullif(trim(current_setting('request.jwt.claim.sub', true)), ''), '')::UUID;
$$;

-- ── Users policies ───────────────────────────────────────────────────────────

CREATE POLICY "users_can_view_own_profile"
    ON public.users FOR SELECT
    USING (id = public.current_user_id());

CREATE POLICY "users_can_update_own_profile"
    ON public.users FOR UPDATE
    USING (id = public.current_user_id());

CREATE POLICY "users_can_insert_own_profile"
    ON public.users FOR INSERT
    WITH CHECK (id = public.current_user_id());

-- ── Digest Settings policies ─────────────────────────────────────────────────

CREATE POLICY "users_manage_own_digest_settings"
    ON public.digest_settings FOR ALL
    USING (user_id = public.current_user_id());

-- ── Items policies ───────────────────────────────────────────────────────────

CREATE POLICY "users_view_own_items"
    ON public.items FOR SELECT
    USING (user_id = public.current_user_id());

CREATE POLICY "users_insert_own_items"
    ON public.items FOR INSERT
    WITH CHECK (user_id = public.current_user_id());

CREATE POLICY "users_update_own_items"
    ON public.items FOR UPDATE
    USING (user_id = public.current_user_id());

CREATE POLICY "users_delete_own_items"
    ON public.items FOR DELETE
    USING (user_id = public.current_user_id());

-- ── Tags policies ────────────────────────────────────────────────────────────

CREATE POLICY "users_manage_own_tags"
    ON public.tags FOR ALL
    USING (user_id = public.current_user_id());

-- ── Categories policies ──────────────────────────────────────────────────────

CREATE POLICY "users_manage_own_categories"
    ON public.categories FOR ALL
    USING (user_id = public.current_user_id());

-- ── Item Tags policies ───────────────────────────────────────────────────────

-- Users can link tags to their own items only
CREATE POLICY "users_manage_own_item_tags"
    ON public.item_tags FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM public.items
            WHERE items.id = item_tags.item_id
              AND items.user_id = public.current_user_id()
        )
    );

-- ── Trigger: auto-update updated_at ───────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER users_updated_at
    BEFORE UPDATE ON public.users
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ── Grant permissions for service role ───────────────────────────────────────
-- The SERVICE ROLE key bypasses RLS, so it can do everything.
-- The ANON key respects RLS, so it can only access data the user owns.

GRANT USAGE ON SCHEMA public TO supabase_service_role;
GRANT ALL ON ALL TABLES IN SCHEMA public TO supabase_service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO supabase_service_role;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO supabase_service_role;

-- Grant to authenticated users
GRANT USAGE ON SCHEMA public TO authenticated;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO authenticated;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO authenticated;