-- Mark My Words — initial database schema
-- Migration: 202608260001_init_mark_my_words.sql
--
-- Dependencies: Supabase (Postgres) with `pgcrypto` for gen_random_uuid().

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- user_logs: every app visit / login is logged here.
-- ---------------------------------------------------------------------------
create table if not exists public.user_logs (
    id          uuid primary key default gen_random_uuid(),
    user_email  text not null,
    action      text not null,
    details     text,
    created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- essay_memory: teacher exemplars and AI grading results.
-- ---------------------------------------------------------------------------
create table if not exists public.essay_memory (
    id                 uuid primary key default gen_random_uuid(),
    student_name       text not null,
    essay_text         text not null,
    rubric_type        text,
    ai_score           numeric,
    score              numeric,
    teacher_feedback   text,
    red_pen_corrections text,
    teacher_email      text,
    embedding          jsonb,
    created_at         timestamptz not null default now()
);

-- Helpful lookup indexes.
create index if not exists user_logs_email_idx      on public.user_logs (user_email);
create index if not exists user_logs_created_at_idx on public.user_logs (created_at desc);
create index if not exists essay_memory_created_at_idx on public.essay_memory (created_at desc);
create index if not exists essay_memory_rubric_idx    on public.essay_memory (rubric_type);

-- ---------------------------------------------------------------------------
-- Row Level Security.
-- The app uses the Supabase anon/authenticated client with its service key for
-- this private grading portal, so RLS is enabled and the app policy allows
-- only authenticated inserts/selects. Tighten the select policy to specific
-- admins in production if tables should not be publicly readable.
-- ---------------------------------------------------------------------------
alter table public.user_logs enable row level security;
alter table public.essay_memory enable row level security;

drop policy if exists "user_logs_insert" on public.user_logs;
create policy "user_logs_insert"
    on public.user_logs
    for insert
    to authenticated
    with check (true);

drop policy if exists "essay_memory_insert" on public.essay_memory;
create policy "essay_memory_insert"
    on public.essay_memory
    for insert
    to authenticated
    with check (true);

drop policy if exists "user_logs_select" on public.user_logs;
create policy "user_logs_select"
    on public.user_logs
    for select
    to authenticated
    using (true);

drop policy if exists "essay_memory_select" on public.essay_memory;
create policy "essay_memory_select"
    on public.essay_memory
    for select
    to authenticated
    using (true);
