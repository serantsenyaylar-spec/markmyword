-- Mark My Words — RLS hardening for EXISTING databases
-- Migration: 202608260002_harden_rls.sql
--
-- Supabase applies each migration file exactly once. Databases that already
-- ran 202608260001_init_mark_my_words.sql (the permissive version) need this
-- follow-up to get the tightened policies; it is fully idempotent, so it is
-- also safe to run against a fresh database that already includes the
-- hardened 202608260001.
--
-- SECURITY MODEL — see 202608260001_init_mark_my_words.sql for the full
-- explanation of Mode A (anon key + Supabase Auth) vs Mode B (service key,
-- server-side only). In Mode B these policies are bypassed by design.

-- Enable RLS (idempotent).
alter table public.user_logs enable row level security;
alter table public.essay_memory enable row level security;

-- Block anonymous access: the public anon key can no longer read either table.
revoke all on table public.user_logs from anon;
revoke all on table public.essay_memory from anon;

-- Helpful lookup index on the teacher scoping column (idempotent).
create index if not exists essay_memory_teacher_idx
    on public.essay_memory (teacher_email);

-- ---------------------------------------------------------------------------
-- user_logs: teachers may insert and read only their own rows.
-- ---------------------------------------------------------------------------
drop policy if exists "user_logs_insert" on public.user_logs;
drop policy if exists "user_logs_insert_own" on public.user_logs;
create policy "user_logs_insert_own"
    on public.user_logs
    for insert
    to authenticated
    with check (user_email = lower(auth.jwt() ->> 'email'));

drop policy if exists "user_logs_select" on public.user_logs;
drop policy if exists "user_logs_select_own" on public.user_logs;
create policy "user_logs_select_own"
    on public.user_logs
    for select
    to authenticated
    using (user_email = lower(auth.jwt() ->> 'email'));

-- ---------------------------------------------------------------------------
-- essay_memory: teachers may insert and read only their own rows.
-- ---------------------------------------------------------------------------
drop policy if exists "essay_memory_insert" on public.essay_memory;
drop policy if exists "essay_memory_insert_own" on public.essay_memory;
create policy "essay_memory_insert_own"
    on public.essay_memory
    for insert
    to authenticated
    with check (teacher_email = lower(auth.jwt() ->> 'email'));

drop policy if exists "essay_memory_select" on public.essay_memory;
drop policy if exists "essay_memory_select_own" on public.essay_memory;
create policy "essay_memory_select_own"
    on public.essay_memory
    for select
    to authenticated
    using (teacher_email = lower(auth.jwt() ->> 'email'));
