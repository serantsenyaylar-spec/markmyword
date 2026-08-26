# Mark My Words — Diagnostics & Security Check Report

**Date:** 2026-08-26 · **Branch:** `arena/01a03d5c-markmyword` · **Target:** `app.py` (1,431 lines), `supabase/migrations/*.sql`, `requirements.txt`
**Toolchain:** Python 3.11 · Bandit 1.9.4 · Ruff 0.16.4 · pip-audit 2.10.1 · Streamlit 1.62.0 AppTest runtime boot test

---

## 0. Remediation Applied (2026-08-26)

All findings below were fixed, verified, and committed on this branch:

| ID | Fix | Where |
|---|---|---|
| D1 | `get_secret()` no longer crashes when no `secrets.toml` exists — falls back to `os.environ` | `app.py` `get_secret()` |
| D2 | Migrated all 13 `use_container_width=` call sites to `width="stretch"`; replaced deprecated `st.components.v1.html` clock with `st.html` (+ `unsafe_allow_javascript=True`) | throughout `app.py` |
| D3 | Pinned all dependencies to exact verified versions (`==`) | `requirements.txt` |
| H1/H2 | RLS rewritten: per-teacher policies scoped to `auth.jwt() ->> 'email'`; `anon` role revoked from both tables; docs updated for anon-key (Mode A) vs service-key (Mode B) deployment | `supabase/migrations/*.sql` |
| H3 | Teacher name/email inputs are now read-only; audit identity locked to OAuth-verified account | `app.py` Tab 1 |
| M1 | ntfy requests attach `Authorization: Bearer NTFY_TOKEN` when a token is configured | `app.py` `send_ntfy_alert()` |
| M2 | Bypass now requires **two** explicit switches (`DEV_AUTH_BYPASS` + `ALLOW_DEV_BYPASS`) and shows a prominent warning when active | `app.py` + secrets template |
| M3 | 10 MB per-file cap in app code + `server.maxUploadSize = 10` | `app.py`, `.streamlit/config.toml` |
| M4 | Magic-byte validation of uploads (spoofed extensions rejected) | `app.py` `_looks_like_extension()` |
| M5 | Google scopes reduced to `spreadsheets` + `drive.file` (full `drive` removed) | `app.py` `get_google_credentials()` |
| L1 | Access logging moved after authentication; hardcoded fallback email removed; anonymous visitors write no audit rows | `app.py` |
| L2 | Silent `except: pass` blocks now log via module logger | `app.py` |
| L3 | Grader rules moved to `system_instruction` (Gemini) / system message (Groq) — student text can no longer override them | `app.py` AI runners |
| L5 | tz-aware timestamps, root-logger call replaced, ruff autofixes applied (32→24, remaining are deliberate catch-alls) | `app.py` |

**Post-fix verification:** Bandit 0 findings · `py_compile` OK · AppTest boot: 0 exceptions, all 9 tabs render, 0 deprecation warnings · no-secrets boot now shows the login gate instead of crashing · one-key bypass correctly refuses to activate · magic-byte validator passes 7/7 cases.

---

## 1. Diagnostics Summary

| Check | Result | Detail |
|---|---|---|
| Python syntax / byte-compile | ✅ PASS | `py_compile app.py` clean |
| Runtime boot test (with secrets) | ✅ PASS | All 9 tabs render, **0 exceptions**, auth gate enforced |
| Runtime boot test (no `secrets.toml`) | ❌ FAIL | App crashes — see D1 |
| Security static analysis (Bandit) | 🟡 2 Low | No Medium/High findings |
| Dependency CVE scan (pip-audit) | ✅ PASS | 0 known vulnerabilities in `requirements.txt` |
| Hardcoded secrets scan (tree + full git history) | ✅ PASS | No API keys, tokens, or private keys found |
| `.env` / `.streamlit/secrets.toml` hygiene | ✅ PASS | Both gitignored and absent from the repo |
| Lint (Ruff) | 🟡 32 findings | All code-quality, none critical |
| Deprecation check | ⚠️ WARN | 2 Streamlit APIs past their removal dates |

### D1 — ❌ App crashes when no `secrets.toml` exists (`app.py:230`)

```python
def get_secret(key_name):
    if hasattr(st, "secrets") and key_name in st.secrets:   # ← raises
        return st.secrets[key_name]
```

Current Streamlit (≥1.40) raises `StreamlitSecretNotFoundError` when **no** secrets file exists — `key_name in st.secrets` triggers a parse. The top-level Supabase init catches this, but every later call to `get_secret()` (API keys, admin emails, Drive/Sheets IDs…) does not, so the app 500s on startup. **Fix:** wrap in try/except and fall back to `os.environ`.

### D2 — ⚠️ Deprecated Streamlit APIs already past removal date

- `use_container_width=True` — deprecated after **2025-12-31** → use `width="stretch"` (many call sites).
- `st.components.v1.html` — deprecated after **2026-06-01** → use `st.iframe` (header clock, `app.py:765`).

Both still work in Streamlit 1.62 but will break on a future upgrade.

### D3 — Dependency pinning

`requirements.txt` uses only lower bounds (`streamlit>=1.41.0`, `pypdf>=4.0.0`, …) with no upper bounds and no lock file. A future `pip install` can silently pull breaking versions (e.g. the Streamlit deprecations above). Consider `==` pins or a lock file.

---

## 2. Security Findings

### 🔴 HIGH

**H1 — Supabase RLS allows any authenticated user to read/write all student data**
`supabase/migrations/202608260001_init_mark_my_words.sql`:

```sql
create policy "essay_memory_select" on public.essay_memory for select to authenticated using (true);
create policy "essay_memory_insert" on public.essay_memory for insert to authenticated with check (true);
```

`essay_memory` contains **student names, full essays, feedback, corrections and teacher emails**; `user_logs` is equally open. Any authenticated teacher in the allowed domain can read every record and insert spoofed ones. The migration's own comment admits this ("Tighten the select policy to specific admins in production"). **Fix:** restrict `select` to admin emails, add `with check (teacher_email = auth.jwt()->>'email')` on insert.

**H2 — Verify which Supabase key the app uses**
The migration comment states the app "uses its service key." If `SUPABASE_KEY` in Streamlit secrets is the **service_role** key, RLS is bypassed entirely and the app has full unrestricted database access. **Fix:** use the `anon`/publishable key with proper RLS, never the service key in a client-facing app.

**H3 — Audit identity is user-editable**
`app.py:934` lets any logged-in teacher overwrite `st.session_state.user_email` (and name) via a free-text input in Tab 1. That value is then written to `user_logs`, `essay_memory.teacher_email`, and the Google Sheet — so audit/attribution records are trivially spoofable. **Fix:** derive audit identity from `st.user.email` (OAuth-verified) and make the inputs read-only.

### 🟠 MEDIUM

**M1 — ntfy.sh alert topic is unauthenticated and guessable**
`send_ntfy_alert()` posts to `https://ntfy.sh/{topic}`. ntfy topics are public by default: anyone who knows/guesses the topic can **read all access alerts (leaking user email addresses)** or push spoofed alerts to the admin's phone. **Fix:** use a long random topic name, an authenticated topic (auth token), or a self-hosted ntfy instance.

**M2 — `DEV_AUTH_BYPASS` is a production backdoor if misconfigured**
If `DEV_AUTH_BYPASS=true` ever lands in production secrets, anyone who reaches the app is silently authenticated as the bypass teacher (the bypass identity is validated, but only against a static email). **Fix:** hard-guard — refuse bypass unless running in a local/dev environment, and log a prominent warning when active (currently only a small sidebar banner).

**M3 — No file-size limits; relies on Streamlit's 200 MB default**
Batch uploads accept up to 5 files with no per-file cap; `.streamlit/config.toml` does not set `server.maxUploadSize`. On a shared server this enables memory/CPU DoS via large PDFs/DOCX (including decompression bombs) before any AI call. **Fix:** set `server.maxUploadSize` (e.g. 10 MB) and/or reject oversized files in `extract_text_from_file`.

**M4 — File type trusted by extension only**
`extract_text_from_file()` branches on the filename suffix. A file with a spoofed extension goes straight into `pypdf`/`docx2txt`/Gemini. Low impact today (parsers are defensive), but magic-byte/type validation would harden the pipeline.

**M5 — Over-broad Google service-account scopes**
`get_google_credentials()` requests full `drive` and `spreadsheets` scopes. **Fix:** restrict to `drive.file` (+ `spreadsheets`). Also the Sheets fallback `client.open("İstek_Schools_Grading_Database")` opens by name — prefer the `SHEET_ID`.

### 🟡 LOW

- **L1 — Pre-auth logging with hardcoded identity:** `log_user_session()` runs before the auth gate and inserts `teacher@istek.k12.tr` as a fallback, so anonymous visitors generate DB rows (log spam / minor write-amplification). Move logging after authentication and use the real identity.
- **L2 — Silent exception swallowing (Bandit B110 ×2):** `try/except: pass` around the ntfy POST (`app.py:77`) and `st.user` extraction (`app.py:294`) hides failures; add logging.
- **L3 — AI prompt-injection hardening:** the anti-injection warning is shipped as a user `Part` inside `contents`, not as `system_instruction`. A crafted essay could attempt to override the grader. **Fix:** pass `config=GenerateContentConfig(system_instruction=SYSTEM_PROMPT, ...)`. (The existing warning line and the `<student_submission>` tagging are good — make them structural.)
- **L4 — Devcontainer disables XSRF protection & CORS** (`--server.enableXsrfProtection false`). Acceptable for local dev only; do not replicate these flags in any production config.
- **L5 — Ruff code-quality findings (32):** blind `except Exception` (17), import ordering, tz-naive `datetime.now()`, unnecessary dict checks. No security impact, worth a cleanup pass.

### ✅ Confirmed good practices

- Secrets never committed (verified across full git history); `.env`/`secrets.toml` gitignored.
- `html.escape()` applied to user-derived name/email in the sidebar; `st.markdown` used without `unsafe_allow_html` for AI output.
- Auth gate rejects non-domain accounts (verified in runtime test).
- Batch quota limits (5 files/batch, 15 papers/session) exist and are enforced.
- Outbound ntfy request has a 5 s timeout; API errors are caught per-engine with Groq fallback.

---

## 3. Recommended Priority Order

All items below have been implemented (see §0 Remediation Applied).

1. **H1/H2:** Lock down Supabase RLS + confirm anon key (student PII exposure). ✅
2. **D1:** Fix `get_secret()` crash for missing secrets file. ✅
3. **M2:** Harden `DEV_AUTH_BYPASS` guard before any production deploy. ✅
4. **H3 + L1:** Use OAuth-verified identity for all audit writes. ✅
5. **M1:** Protect the ntfy topic. ✅
6. **M3/M4:** Add upload size limits & type validation. ✅
7. **D2:** Migrate off deprecated Streamlit APIs. ✅
