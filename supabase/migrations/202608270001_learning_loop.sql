-- Mark My Words — learning loop
-- Migration: 202608270001_learning_loop.sql
--
-- Adds grading-calibration storage and retains one legacy table for historical
-- compatibility:
--
--   1. transcript_corrections is retained only for previously stored records.
--      The Azure Read integration does not read or write this table, build a
--      glossary from it, or claim that teacher edits change Azure OCR.
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
-- transcript_corrections: legacy teacher-correction records.
--
-- The table remains to avoid destroying historical records in an existing
-- database. Current application code does not query or write it, and no Azure
-- OCR request incorporates entries from this table.
-- ---------------------------------------------------------------------------
create table if not exists public.transcript_corrections (
    id             uuid primary key default gen_random_uuid(),
    teacher_email  text not null,
    class_tag      text,
    source_file    text,
    wrong_text     text not null,
    right_text     text not null,
    -- Retained historical usage count; it is not used as an Azure OCR input.
    hit_count      integer not null default 1,
    created_at     timestamptz not null default now()
);

create index if not exists transcript_corrections_teacher_idx
    on public.transcript_corrections (teacher_email);
create index if not exists transcript_corrections_class_idx
    on public.transcript_corrections (teacher_email, class_tag);
create index if not exists transcript_corrections_hits_idx
    on public.transcript_corrections (hit_count desc);

-- Legacy uniqueness rule retained with the historical records.
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
