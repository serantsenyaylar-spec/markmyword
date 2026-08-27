-- Mark My Words — learning loop
-- Migration: 202608270001_learning_loop.sql
--
-- Adds the storage the app needs to IMPROVE with use, in two independent ways:
--
--   1. transcript_corrections — when a teacher fixes a misread word in an AI
--      transcript, the (wrong -> right) pair is stored. Recurring corrections
--      for a class become a glossary that is fed into later transcription
--      prompts, so student names and class-specific vocabulary stop being
--      misread. This is retrieval-based adaptation, NOT model training: no
--      weights change, results improve immediately, and nothing is shared
--      between teachers.
--
--   2. essay_memory.embedding (already written by the app but, until now,
--      never read back) becomes the source of grading calibration examples.
--      The closest previously-graded essays WITH the teacher's final score are
--      shown to the grader as worked examples, so the AI drifts toward this
--      teacher's marking standard.
--
-- SECURITY MODEL — see 202608260001_init_mark_my_words.sql. Mode B
-- (service_role, server-side) bypasses RLS; the policies below apply in
-- Mode A and keep every teacher's data private to that teacher.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- transcript_corrections: teacher-verified handwriting fixes.
--
-- One row per (wrong phrase -> corrected phrase) observed in a real paper.
-- `class_tag` scopes a glossary to a class (e.g. "10A"), because a name like
-- "Şevval" recurs within one class and is the exact kind of token that
-- generic OCR gets wrong every single time.
-- ---------------------------------------------------------------------------
create table if not exists public.transcript_corrections (
    id             uuid primary key default gen_random_uuid(),
    teacher_email  text not null,
    class_tag      text,
    source_file    text,
    wrong_text     text not null,
    right_text     text not null,
    -- How many times this same fix has been confirmed. High-count entries are
    -- the most valuable glossary hints.
    hit_count      integer not null default 1,
    created_at     timestamptz not null default now()
);

create index if not exists transcript_corrections_teacher_idx
    on public.transcript_corrections (teacher_email);
create index if not exists transcript_corrections_class_idx
    on public.transcript_corrections (teacher_email, class_tag);
create index if not exists transcript_corrections_hits_idx
    on public.transcript_corrections (hit_count desc);

-- The same misreading should accumulate on one row rather than duplicate.
create unique index if not exists transcript_corrections_unique
    on public.transcript_corrections (teacher_email, coalesce(class_tag, ''), wrong_text, right_text);

-- ---------------------------------------------------------------------------
-- essay_memory: add a class tag so exemplars can be filtered per class, and
-- record whether a transcript came from handwriting recognition.
-- ---------------------------------------------------------------------------
alter table public.essay_memory
    add column if not exists class_tag text;

alter table public.essay_memory
    add column if not exists was_handwritten boolean not null default false;

create index if not exists essay_memory_class_idx
    on public.essay_memory (teacher_email, class_tag);

-- ---------------------------------------------------------------------------
-- Row Level Security.
-- ---------------------------------------------------------------------------
alter table public.transcript_corrections enable row level security;

revoke all on table public.transcript_corrections from anon;

drop policy if exists "transcript_corrections_insert_own" on public.transcript_corrections;
create policy "transcript_corrections_insert_own"
    on public.transcript_corrections
    for insert
    to authenticated
    with check (teacher_email = lower(auth.jwt() ->> 'email'));

drop policy if exists "transcript_corrections_select_own" on public.transcript_corrections;
create policy "transcript_corrections_select_own"
    on public.transcript_corrections
    for select
    to authenticated
    using (teacher_email = lower(auth.jwt() ->> 'email'));

drop policy if exists "transcript_corrections_update_own" on public.transcript_corrections;
create policy "transcript_corrections_update_own"
    on public.transcript_corrections
    for update
    to authenticated
    using (teacher_email = lower(auth.jwt() ->> 'email'))
    with check (teacher_email = lower(auth.jwt() ->> 'email'));
