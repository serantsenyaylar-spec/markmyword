-- Mark My Words — ensure the grading-calibration embedding column exists
-- Migration: 202608300002_ensure_essay_memory_embedding.sql
--
-- The learning loop (app.py: _embed_text / build_calibration_text) stores a
-- Gemini "gemini-embedding-001" vector on each locked exemplar in
-- essay_memory.embedding and later reads it back to pick the most similar
-- previously-graded essays as few-shot calibration examples.
--
-- The column was declared in the original 001 init table but was never given a
-- constraint, index, or documentation, and a database created from a hand-run
-- or partial migration sequence could lack it. This migration makes the column
-- an explicit, idempotent part of the schema so every deployment — including a
-- fresh Mode A build — has the storage the calibration query expects.
--
-- Idempotent by design: it is safe to run on a project where the column already
-- exists (e.g. one already migrated with 202608300001 or the init script).

-- 1. Ensure the column exists with the type the app writes/reads (jsonb list
--    of floats). A NULL embedding means "no vector was captured", which the
--    application already tolerates and falls back to recency-based calibration.
alter table public.essay_memory
    add column if not exists embedding jsonb;

-- 2. Document the column's contract so future changes do not silently break
--    the calibration lookup.
comment on column public.essay_memory.embedding is
    'Gemini gemini-embedding-001 vector (jsonb list of floats) for grading '
    'calibration; NULL when no embedding was captured. Not a provider response '
    'or credential.';

-- 3. A GIN index keeps future jsonb containment/array queries cheap. Cosine
--    similarity itself is computed in application code against the full list,
--    so no vector extension is required here.
create index if not exists essay_memory_embedding_gin_idx
    on public.essay_memory using gin (embedding);

-- 4. Existing rows are left untouched: their scores and feedback remain valid,
--    and calibration simply skips rows without an embedding (build_calibration_text
--    already falls back to most-recent when no vector is present).
