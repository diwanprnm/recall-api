-- ============================================================================
-- Migration: 007_local_auth.sql
-- Purpose: decouple Recall from Supabase cloud.
--   1. Add password_hash to users (custom auth, bcrypt)
--   2. Disable RLS on all tables (isolation is now app-enforced via user_id filter)
--   3. Drop Supabase RLS policies
--   4. Repoint search RPCs to take p_user_id (no auth.uid()/current_user_id())
-- Run against the existing local postgres (already has schema from 002/003/006).
-- Idempotent.
-- ============================================================================

-- ── 1. password_hash column ───────────────────────────────────────────────────
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS password_hash TEXT;

-- ── 2. Drop RLS policies (created by 002 + 006) ───────────────────────────────
DROP POLICY IF EXISTS "users_can_view_own_profile"        ON public.users;
DROP POLICY IF EXISTS "users_can_update_own_profile"      ON public.users;
DROP POLICY IF EXISTS "users_can_insert_own_profile"      ON public.users;
DROP POLICY IF EXISTS "users_manage_own_digest_settings"  ON public.digest_settings;
DROP POLICY IF EXISTS "users_view_own_items"              ON public.items;
DROP POLICY IF EXISTS "users_insert_own_items"            ON public.items;
DROP POLICY IF EXISTS "users_update_own_items"            ON public.items;
DROP POLICY IF EXISTS "users_delete_own_items"            ON public.items;
DROP POLICY IF EXISTS "users_manage_own_tags"             ON public.tags;
DROP POLICY IF EXISTS "users_manage_own_categories"       ON public.categories;
DROP POLICY IF EXISTS "users_manage_own_item_tags"        ON public.item_tags;
DROP POLICY IF EXISTS "users_item_tags_join_check"        ON public.item_tags;

-- ── 3. Disable RLS on all tables ──────────────────────────────────────────────
ALTER TABLE public.users          DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.digest_settings DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.items          DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.tags           DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.categories     DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.item_tags      DISABLE ROW LEVEL SECURITY;

-- ── 4a. match_items(p_user_id, ...) ───────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.match_items(
    p_user_id        UUID,
    query_embedding  VECTOR(1536),
    match_threshold  FLOAT   DEFAULT 0.7,
    match_count      INT     DEFAULT 20,
    filter_platform  TEXT    DEFAULT NULL,
    filter_tags      TEXT[]  DEFAULT NULL
)
RETURNS TABLE (
    id              UUID,
    user_id         UUID,
    url             TEXT,
    platform        TEXT,
    original_id     TEXT,
    title           TEXT,
    text            TEXT,
    author          TEXT,
    author_handle   TEXT,
    author_avatar   TEXT,
    thumbnail_url   TEXT,
    summary         TEXT,
    key_points      JSONB,
    sentiment       TEXT,
    quality_score   INTEGER,
    saved_at        TIMESTAMPTZ,
    read_at         TIMESTAMPTZ,
    is_favorite     BOOLEAN,
    is_archived     BOOLEAN,
    category_id     UUID,
    similarity      FLOAT,
    tags            TEXT[]
)
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    match_count := LEAST(match_count, 100);
    RETURN QUERY
    WITH
    vector_matches AS (
        SELECT
            i.id,
            i.embedding <=> query_embedding AS distance,
            1 - (i.embedding <=> query_embedding) AS similarity
        FROM items i
        WHERE i.embedding IS NOT NULL
          AND i.user_id = p_user_id
          AND i.is_archived = false
          AND (filter_platform IS NULL OR i.platform = filter_platform)
        ORDER BY i.embedding <=> query_embedding
        LIMIT match_count * 2
    ),
    tagged_items AS (
        SELECT item_id
        FROM item_tags it
        JOIN tags t ON t.id = it.tag_id
        WHERE filter_tags IS NULL
           OR t.name = ANY(filter_tags)
        GROUP BY it.item_id
        HAVING filter_tags IS NULL
            OR COUNT(DISTINCT t.name) >= array_length(filter_tags, 1)
    )
    SELECT
        i.id, i.user_id, i.url, i.platform, i.original_id, i.title, i.text,
        i.author, i.author_handle, i.author_avatar, i.thumbnail_url, i.summary,
        i.key_points, i.sentiment, i.quality_score, i.saved_at, i.read_at,
        i.is_favorite, i.is_archived, i.category_id,
        vm.similarity,
        COALESCE(
            array_agg(DISTINCT t.name) FILTER (WHERE t.name IS NOT NULL),
            ARRAY[]::TEXT[]
        ) AS tags
    FROM vector_matches vm
    JOIN items i ON i.id = vm.id
    LEFT JOIN item_tags it ON it.item_id = i.id
    LEFT JOIN tags t ON t.id = it.tag_id
    WHERE vm.similarity >= match_threshold
      AND (filter_tags IS NULL OR vm.id IN (SELECT item_id FROM tagged_items))
    GROUP BY
        i.id, i.user_id, i.url, i.platform, i.original_id, i.title, i.text,
        i.author, i.author_handle, i.author_avatar, i.thumbnail_url, i.summary,
        i.key_points, i.sentiment, i.quality_score, i.saved_at, i.read_at,
        i.is_favorite, i.is_archived, i.category_id, vm.similarity
    ORDER BY vm.similarity DESC
    LIMIT match_count;
END;
$$;

-- ── 4b. hybrid_search(p_user_id, ...) ─────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.hybrid_search(
    p_user_id        UUID,
    search_query     TEXT,
    query_embedding  VECTOR(1536),
    match_threshold  FLOAT   DEFAULT 0.6,
    match_count      INT     DEFAULT 20,
    filter_platform  TEXT    DEFAULT NULL
)
RETURNS TABLE (
    id          UUID,
    url         TEXT,
    platform    TEXT,
    title       TEXT,
    text        TEXT,
    summary     TEXT,
    saved_at    TIMESTAMPTZ,
    rank        FLOAT,
    tags        TEXT[]
)
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    RETURN QUERY
    WITH
    text_matches AS (
        SELECT
            i.id,
            ts_rank(
                to_tsvector('english', coalesce(i.title, '') || ' ' || coalesce(i.text, '')),
                plainto_tsquery('english', search_query)
            ) AS text_rank
        FROM items i
        WHERE i.user_id = p_user_id
          AND i.is_archived = false
          AND to_tsvector('english', coalesce(i.title, '') || ' ' || coalesce(i.text, ''))
              @@ plainto_tsquery('english', search_query)
    ),
    vector_matches AS (
        SELECT
            i.id,
            1 - (i.embedding <=> query_embedding) AS similarity
        FROM items i
        WHERE i.user_id = p_user_id
          AND i.is_archived = false
          AND i.embedding IS NOT NULL
          AND (1 - (i.embedding <=> query_embedding)) >= match_threshold
    )
    SELECT
        i.id, i.url, i.platform, i.title, left(i.text, 500), i.summary,
        i.saved_at,
        COALESCE(vm.similarity, 0) * 0.7 + COALESCE(tm.text_rank, 0) * 0.3 AS rank,
        array_agg(DISTINCT t.name) FILTER (WHERE t.name IS NOT NULL) AS tags
    FROM items i
    LEFT JOIN vector_matches vm ON vm.id = i.id
    LEFT JOIN text_matches tm ON tm.id = i.id
    LEFT JOIN item_tags it ON it.item_id = i.id
    LEFT JOIN tags t ON t.id = it.tag_id
    WHERE (vm.id IS NOT NULL OR tm.id IS NOT NULL)
      AND i.is_archived = false
      AND (filter_platform IS NULL OR i.platform = filter_platform)
    GROUP BY i.id, i.url, i.platform, i.title, i.text, i.summary, i.saved_at, vm.similarity, tm.text_rank
    ORDER BY rank DESC
    LIMIT match_count;
END;
$$;

-- ── 4c. get_resurfacing_candidates(p_user_id, ...) ────────────────────────────
CREATE OR REPLACE FUNCTION public.get_resurfacing_candidates(
    p_user_id           UUID,
    days_since_last_read INT DEFAULT 7,
    limit_count          INT DEFAULT 20
)
RETURNS TABLE (
    id              UUID,
    title           TEXT,
    summary         TEXT,
    url             TEXT,
    platform        TEXT,
    thumbnail_url   TEXT,
    saved_at        TIMESTAMPTZ,
    quality_score   INTEGER,
    days_unread     INT
)
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    RETURN QUERY
    SELECT
        i.id, i.title, i.summary, i.url, i.platform, i.thumbnail_url, i.saved_at,
        i.quality_score,
        EXTRACT(DAY FROM now() - COALESCE(i.read_at, i.saved_at))::INT AS days_unread
    FROM items i
    WHERE i.user_id = p_user_id
      AND i.is_archived = false
      AND (
          i.read_at IS NULL
          OR EXTRACT(DAY FROM now() - i.read_at) >= days_since_last_read
      )
    ORDER BY i.quality_score DESC NULLS LAST, random()
    LIMIT limit_count;
END;
$$;

-- ── 5. Grants to the DB owner role (was authenticated / supabase_service_role) ─
GRANT EXECUTE ON FUNCTION public.match_items(UUID, VECTOR, FLOAT, INT, TEXT, TEXT[]) TO recall;
GRANT EXECUTE ON FUNCTION public.hybrid_search(UUID, TEXT, VECTOR, FLOAT, INT, TEXT) TO recall;
GRANT EXECUTE ON FUNCTION public.get_resurfacing_candidates(UUID, INT, INT) TO recall;
