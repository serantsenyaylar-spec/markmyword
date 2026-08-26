-- Mark My Words — initial database schema
-- Migration: 202608260001_init_mark_my_words.sql
--
-- Dependencies: Supabase (Postgres) with `pgcrypto` for gen_random_uuid().
--
-- SECURITY MODEL — READ CAREFULLY
-- ------------------------------
-- Two supported deployment modes:
--
--   MODE A (recommended, target state):
--     The app authenticates users with Supabase Auth (or passes a user JWT)
--     and uses the anon/publishable key. Requests then carry role
--     `authenticated` with the user's email in the JWT, and the policies
--     below enforce that a teacher can only read/insert rows belonging to
--     their own verified email address.
--
--   MODE B (legacy, current):
--     The app uses the service_role key from SERVER-SIDE code only (the key
--     never reaches a browser). Supabase bypasses RLS for service_role, so
--     the policies below are inert in this mode; access control relies on
--     the app's Google OAuth gate. Never ship the service_role key to any
--     client, and migrate to Mode A as soon as feasible.
--
-- The `anon` role has NO policies on these tables, so RLS default-deny
-- blocks all anonymous access even though the anon key is public.

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
create index if not exists essay_memory_teacher_idx   on public.essay_memory (teacher_email);

-- ---------------------------------------------------------------------------
-- Row Level Security (Mode A).
--
-- NOTE on `with check`: the insert policies derive the owner from the
-- verified JWT email, so a teacher can never attribute rows to a different
-- account. If you deploy with Mode B (service key), these policies are
-- bypassed — keep the app's OAuth gate as the only access control.
-- ---------------------------------------------------------------------------
alter table public.user_logs enable row level security;
alter table public.essay_memory enable row level security;

-- Revoke any default grants to anon on the public schema tables so that
-- anonymous requests (which use the public anon key) cannot read PII.
revoke all on table public.user_logs from anon;
revoke all on table public.essay_memory from anon;

-- user_logs: teachers may insert and read only their own rows.
drop policy if exists "user_logs_insert" on public.user_logs;
create policy "user_logs_insert_own"
    on public.user_logs
    for insert
    to authenticated
    with check (user_email = lower(auth.jwt() ->> 'email'));

drop policy if exists "user_logs_select" on public.user_logs;
create policy "user_logs_select_own"
    on public.user_logs
    for select
    to authenticated
    using (user_email = lower(auth.jwt() ->> 'email'));

-- essay_memory: teachers may insert and read only their own rows.
drop policy if exists "essay_memory_insert" on public.essay_memory;
create policy "essay_memory_insert_own"
    on public.essay_memory
    for insert
    to authenticated
    with check (teacher_email = lower(auth.jwt() ->> 'email'));

drop policy if exists "essay_memory_select" on public.essay_memory;
create policy "essay_memory_select_own"
    on public.essay_memory
    for select
    to authenticated
    using (teacher_email = lower(auth.jwt() ->> 'email'));
