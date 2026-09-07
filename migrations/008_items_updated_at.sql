-- Add updated_at to items (missed in 002_schemas.sql).
-- The API's item mutations (POST upsert, PATCH, archive, favorite, reanalyse)
-- all set updated_at = now(); without the column those queries 500.
-- The trigger keeps it current on every UPDATE, matching the users table pattern.

ALTER TABLE public.items
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

DROP TRIGGER IF EXISTS items_updated_at ON public.items;

CREATE TRIGGER items_updated_at
    BEFORE UPDATE ON public.items
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
