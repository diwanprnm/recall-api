-- ============================================================================
-- Migration: 001_extensions.sql
-- Description: Enable required PostgreSQL extensions
-- Run this FIRST before creating any tables
-- ============================================================================

-- Enable vector extension for semantic search (pgvector)
CREATE EXTENSION IF NOT EXISTS vector;

-- Enable UUID generation (CUID replacement — Supabase uses cuid(), we use uuid)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Enable unaccent for accent-insensitive text search (hybrid search)
CREATE EXTENSION IF NOT EXISTS unaccent;

-- Confirm extensions are available
SELECT extname, extversion
FROM pg_extension
WHERE extname IN ('vector', 'pgcrypto', 'unaccent');