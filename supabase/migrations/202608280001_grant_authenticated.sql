-- Mark My Words — explicit table grants for the `authenticated` role
-- Migration: 202608280001_grant_authenticated.sql
--
-- WHY THIS MIGRATION EXISTS
-- ------------------------
-- Migrations 001-003 create correct RLS policies but never GRANT anything to
-- the `authenticated` role. That was verified on PostgreSQL 17: with no grant,
-- an insert fails with
--
--     ERROR: permission denied for table user_logs
--
-- and the row-level security policies are never even evaluated. The policies
-- look right in `pg_policies` and do nothing.
--
-- The project worked anyway only because Supabase projects carry ambient
-- default privileges on the `public` schema. Those are a property of the
-- Supabase project, not of this repository: if they change, or if the project
-- was created under stricter defaults, Mode A (anon key + user JWT) breaks
-- silently while every policy still looks correct. Granting explicitly removes
-- that dependency and makes the security model self-contained.
--
-- In Mode B (service_role key, server-side) these grants are irrelevant
-- because service_role bypasses RLS — but they cost nothing and are what makes
-- the documented Mode A migration path actually work.
--
-- Least privilege: only the operations the app performs.
--   user_logs              : insert (login/session audit), select (admin feed)
--   essay_memory           : insert (lock a grade), select (portfolio lookup)
--   transcript_corrections : legacy historical table; later migration retires
--                            new authenticated writes without deleting rows
-- No DELETE and no UPDATE anywhere else: nothing in app.py issues them.

grant usage on schema public to authenticated;

grant select, insert on public.user_logs to authenticated;
grant select, insert on public.essay_memory to authenticated;
grant select, insert, update on public.transcript_corrections to authenticated;

-- Defense in depth, restated so a database built from scratch is covered even
-- if 001/002 are ever run against a project where the revokes were skipped.
-- The `anon` role must hold nothing: its key is public.
revoke all on table public.user_logs from anon;
revoke all on table public.essay_memory from anon;
revoke all on table public.transcript_corrections from anon;
