-- ============================================================================
-- Migration: 005_user_auto_provision.sql
-- Description: Auto-create public.users row when new auth.users is inserted.
--              This is essential for RLS to work — auth.uid() resolves to a
--              user that must exist in the public.users table (per our RLS
--              policies like "users_view_own_profile" USING id = auth.uid()).
-- Run AFTER 002_schemas.sql.
-- ============================================================================

-- ── Trigger function: copy auth.users → public.users ─────────────────────────

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    INSERT INTO public.users (id, email, name, avatar_url)
    VALUES (
        NEW.id,
        NEW.email,
        NEW.raw_user_meta_data->>'full_name',
        NEW.raw_user_meta_data->>'avatar_url'
    )
    ON CONFLICT (id) DO UPDATE SET
        email = EXCLUDED.email,
        name = COALESCE(EXCLUDED.name, public.users.name),
        avatar_url = COALESCE(EXCLUDED.avatar_url, public.users.avatar_url);

    -- Also auto-provision default digest settings
    INSERT INTO public.digest_settings (user_id)
    VALUES (NEW.id)
    ON CONFLICT (user_id) DO NOTHING;

    RETURN NEW;
END;
$$;

-- ── Trigger: fire on auth.users INSERT ───────────────────────────────────────

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;

CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- ── Backfill: existing users that signed up before this trigger ────────────
-- Run once to populate public.users for any users that exist in auth.users
-- but not yet in public.users.

INSERT INTO public.users (id, email, name, avatar_url)
SELECT
    au.id,
    au.email,
    au.raw_user_meta_data->>'full_name',
    au.raw_user_meta_data->>'avatar_url'
FROM auth.users au
ON CONFLICT (id) DO NOTHING;

-- Backfill digest settings too
INSERT INTO public.digest_settings (user_id)
SELECT id FROM public.users
ON CONFLICT (user_id) DO NOTHING;

-- ── GRANT EXECUTE on the function to the supabase_auth_admin ────────────────
-- This is so the auth schema can invoke it (since auth.users is owned by supabase_auth_admin).
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'supabase_auth_admin') THEN
        GRANT USECAGE ON SCHEMA public TO supabase_auth_admin;
        GRANT INSERT, UPDATE ON public.users TO supabase_auth_admin;
        GRANT INSERT ON public.digest_settings TO supabase_auth_admin;
    END IF;
END
$$;