# Mark My Words — Diagnostics & Security Check

**Run date:** 2026-08-26 (UTC)

**Branch:** `arena/01a03e1b-markmyword`

**Scope:** `app.py`, dependency and deployment configuration, Supabase migrations, and tracked Git history

**Toolchain:** Python 3.11.2 · Streamlit 1.62.0 · Bandit 1.9.4 · Ruff 0.16.4 · pip-audit 2.10.1

## Executive summary

The application passed syntax, dependency-integrity, vulnerability, secret-hygiene, and static-security checks. No known dependency vulnerabilities, Bandit findings, committed credentials, or Git object-integrity problems were found.

This check also remediated four meaningful issues: unauthenticated visitors no longer receive a raw Streamlit-secrets error, development bypasses now fail closed unless the environment is explicitly local, non-admin portfolio searches are restricted to that teacher's rows, and the development container no longer disables Streamlit CORS/XSRF protections.

The principal **remaining deployment risk** is the Supabase authentication model: the app's Google OAuth session is not exchanged for a Supabase JWT. Therefore, the RLS policies only apply if a separate Supabase Auth/JWT integration is added. The currently workable server-side model uses a service-role key, which bypasses RLS by design and makes the Streamlit app's authentication and authorization checks the data boundary. See [R1](#r1--supabase-authentication-model--medium) before production deployment.

---

## 1. Diagnostics results

| Check | Result | Evidence |
|---|---|---|
| Python AST parse and byte compilation | ✅ PASS | `python -m py_compile app.py` completed successfully. |
| Dependency installation and consistency | ✅ PASS | Exact pins installed in an isolated Python 3.11 virtual environment; `pip check` reported no broken requirements. |
| Dependency CVE audit | ✅ PASS | `pip-audit -r requirements.txt`: **No known vulnerabilities found**. |
| Python security SAST | ✅ PASS | `bandit -r app.py`: **0 Low / 0 Medium / 0 High** findings. |
| Runtime — unauthenticated/no-secrets path | ✅ PASS | Streamlit `AppTest`: 0 exceptions, no raw configuration error, login gate rendered. |
| Runtime — invalid bypass configuration | ✅ PASS | Both bypass switches with no local `APP_ENV` leave the login gate active. |
| Runtime — explicit local bypass | ✅ PASS | `APP_ENV=development` plus both switches rendered the teacher UI with 0 exceptions and the expected validated identity. |
| Upload type guard | ✅ PASS | Magic-byte checks passed **8/8** for PDF, DOCX, PNG, JPEG, and TXT cases. |
| Secret hygiene | ✅ PASS | No credential-format matches in tracked source or reachable Git history; `.env` and `.streamlit/secrets.toml` are ignored. |
| Git integrity / patch whitespace | ✅ PASS | `git fsck --no-reflogs --full` and `git diff --check` completed cleanly. |
| Lint | 🟡 ADVISORY | Ruff reports **24** findings: 23 broad `Exception` handlers (`BLE001`) and one nested-condition simplification (`SIM102`). No correctness or security error was reported by the tool. |
| CodeQL workflow | ✅ CONFIGURED | Python CodeQL runs on pushes/PRs to `main` and weekly. It was not run locally in this review. |
| Dependabot configuration | ✅ FIXED | The package ecosystem was blank (invalid); it now correctly targets `pip` weekly. |

### Runtime coverage and limits

The dynamic tests deliberately used no real credentials. They verify the login gate and local-only development pathway without sending student content or credentials to external services. This review did **not** execute a real Google OAuth login, call Gemini/Groq, access Google Drive/Sheets, connect to a production Supabase project, or apply the SQL migrations. Those integrations require a staging environment and non-production test credentials.

---

## 2. Security remediation applied

| ID | Severity before fix | Change | Verification |
|---|---|---|---|
| F1 | Low — information disclosure | Moved safe secret retrieval before Supabase initialization, removed direct `st.secrets` calls from that path, and log initialization errors server-side instead of showing filesystem/configuration details to anonymous users. | No-secrets `AppTest` has 0 errors and renders only the login gate. |
| F2 | Medium — authentication bypass misconfiguration | A bypass now requires **all three**: `APP_ENV` in `development`/`dev`/`local`/`test`, `DEV_AUTH_BYPASS=true`, and `ALLOW_DEV_BYPASS=true`. | Flags without local `APP_ENV` fail closed; explicit development configuration passes. |
| F3 | Medium — cross-teacher PII visibility in server-role mode | Non-admin Student Portfolio queries now include `teacher_email = USER_EMAIL`; only administrators retain cross-teacher search. | Source review confirms the filter is applied before `ilike` and query execution. |
| F4 | Low — insecure development defaults | Removed `--server.enableCORS false` and `--server.enableXsrfProtection false` plus their environment overrides from the devcontainer. | Configuration review. |
| F5 | Operational security | Replaced blank Dependabot ecosystem with `pip`, added complete Streamlit Google OAuth keys to the untracked secret template, and replaced placeholder `SECURITY.md` content with a private reporting policy. | TOML template parsing and configuration review. |

### Existing controls confirmed

- Dependency versions are exact-pinned in `requirements.txt`.
- Per-file uploads are capped at 10 MB in both Streamlit config and application code; batches are capped at 5 files and sessions at 15 papers.
- PDF, DOCX, PNG, and JPEG uploads are checked against expected magic bytes before parsing; TXT is intentionally signature-free.
- Google Workspace scopes are limited to `spreadsheets` and `drive.file`.
- The app normalizes OAuth email addresses and checks the configured school domain/admin allow-list before rendering the teacher portal.
- Audit identity inputs are disabled and persistence uses the authenticated session identity.
- AI grader instructions are supplied as Gemini system instructions / Groq system messages; student content is structurally separated from the grading rules.
- ntfy uses a five-second timeout and supports a bearer token for protected topics.
- User-derived sidebar identity values are HTML-escaped; the existing `unsafe_allow_html` uses are static UI templates.

---

## 3. Remaining risks and required deployment decisions

### R1 — Supabase authentication model — **Medium**

The repository includes RLS policies based on `auth.jwt() ->> 'email'`. However, `app.py` creates a Supabase client from a project URL/key and never supplies a Supabase Auth user token. Google/Streamlit authentication by itself does not populate Supabase's `auth.jwt()` claims.

Consequences:

- With an **anon/publishable key**, requests run as `anon`; the migrations revoke that role and database reads/writes will fail.
- With a **service-role key**, requests run server-side and work, but RLS is bypassed. The app's Google OAuth gate and code-level scopes are then the effective authorization boundary.

The secret template now documents the only current working server-side configuration and the non-admin portfolio filter added in F3. Before a production release, choose one architecture explicitly:

1. **Preferred defense-in-depth:** add a trusted Supabase Auth/JWT integration, use the anon/publishable key, and exercise the existing RLS policies with integration tests; or
2. **Server-side service role:** retain the service-role key only in the host's server-side Streamlit secrets, never in browser JavaScript or a client build, and add staging tests for every data query/role boundary.

Also confirm the actual deployment never emits `SUPABASE_KEY` in HTML, JavaScript bundles, logs, or a browser-visible environment variable.

### R2 — Uploaded document resource exhaustion — **Low to Medium**

The 10 MB upload cap and signature check substantially reduce risk, but a small compressed PDF/DOCX can still expand during parsing or yield excessive extracted text. The app does not currently impose PDF page, DOCX archive-uncompressed-size, or extracted-text limits.

**Recommendation:** add limits before parser/model use (for example, PDF page count, DOCX archive member count and uncompressed size, and maximum extracted characters). Exercise those guards with decompression-bomb and oversized-text fixtures in a staging test.

### R3 — Student data sent to third-party AI providers — **Medium privacy/compliance consideration**

Student essays, names derived from file names, and teacher feedback can be sent to Gemini and/or Groq. No code vulnerability was found here, but production use requires the school's data-processing, consent, retention, and regional-transfer requirements to be confirmed. Avoid putting student identifiers in upload file names where operationally possible.

### R4 — Broad exception handling — **Low maintainability/observability**

Ruff's 23 `BLE001` notices are mostly boundaries around APIs, parsing, and optional integrations. They are not Bandit findings, but broad catches can mask unexpected programming errors and several still use `print()` rather than structured logging.

**Recommendation:** replace them incrementally with expected SDK/network/parser exception types, emit structured logs without secrets or student text, and reserve a final broad exception only at UI boundaries.

---

## 4. Production readiness checklist

- [ ] Configure Google OAuth in the real untracked `.streamlit/secrets.toml`: `cookie_secret`, client ID/secret, and the exact HTTPS redirect URI.
- [ ] Keep `APP_ENV="production"`; do not set either development-bypass switch in hosted secrets.
- [ ] Make the R1 Supabase architecture decision and test it against staging data.
- [ ] Keep all API, OAuth, ntfy, Google service-account, and Supabase service-role credentials server-side and out of Git.
- [ ] Use a private/protected random ntfy topic and set `NTFY_TOKEN` if notifications carry email addresses.
- [ ] Set a deployment-level reverse-proxy/body-size limit in addition to the Streamlit 10 MB cap.
- [ ] Add parser/text-size limits from R2 and test malicious fixture handling.
- [ ] Add a CI job for `py_compile`, Bandit, Ruff, `pip-audit`, and the Streamlit gate tests; CodeQL and Dependabot are already configured.
- [ ] Run a staging smoke test for OAuth, Supabase permissions, Drive/Sheets, and both AI engines before release.

---

## Commands used

```bash
python -m py_compile app.py
python -m pip check
bandit -q -r app.py
pip-audit -r requirements.txt
ruff check app.py
git fsck --no-reflogs --full
git diff --check
```

Dynamic checks used `streamlit.testing.v1.AppTest` for the unauthenticated gate, a fail-closed bypass configuration, and an explicitly local bypass configuration. Credential-format scans covered tracked source and reachable Git history.

---

## Re-check — 2026-08-27 (branch `arena/01a0424b-markmyword`)

**Scope:** full repository re-verification on the merged `main` (identical to `arena/handoff`; both at `635de01`), plus targeted fixes.

### Verification results

| Check | Result |
|---|---|
| Clean-venv install of the exact pins (Python 3.11.2) | ✅ PASS — `pip check` clean |
| `python -m py_compile app.py` | ✅ PASS |
| `bandit -r app.py` | ✅ PASS — 0 findings |
| `pip-audit -r requirements.txt` | ✅ PASS — 0 known vulnerabilities |
| `ruff check app.py` | 🟡 ADVISORY — unchanged baseline: 23 × BLE001, 1 × SIM102 (see R4) |
| AppTest: no-secrets login gate | ✅ PASS — 0 exceptions, gate rendered |
| AppTest: explicit local dev bypass | ✅ PASS — 0 exceptions, teacher UI + admin tabs rendered |
| AppTest: bypass with `APP_ENV=production` | ✅ PASS — fails closed at login gate |
| CodeQL workflow history | ✅ All recent runs on `main` succeeded |
| Git integrity (`git fsck`, `git diff --check`) | ✅ PASS |
| Branch state | ℹ️ `main` and `arena/handoff` point at the same commit; nothing to reconcile |

### Issues fixed in this pass

| ID | Severity | Change |
|---|---|---|
| F6 | Medium (data quality) | `log_user_login` read the `login_notified` run-once guard but nothing inside it ever set the flag — the only assignment lived at the end of the unrelated `send_ntfy_alert` helper. With the database configured, every Streamlit rerun inserted another duplicate "Logged in as …" row into `user_logs`. The guard is now set inside `log_user_login` exactly when the audit row is written (and set on the no-database path); a failed insert still retries on the next run. `send_ntfy_alert` no longer mutates login-audit state. Verified by a stubbed regression test: 5 simulated reruns produce exactly 1 audit row. |
| F7 | Low (latent `NameError`) | `from google.genai import types` was imported mid-file *below* `extract_text_from_image`, which uses `types.Part`. It only worked because call sites happened to run after the import line. The import now lives with the other top-level imports. |
| F8 | Low (correctness of defaults) | The `preset_template` session default referenced a preset name that no longer exists ("Guided Essay Writing (120–150 words)"); it now matches the actual first preset, "Guided Paragraph Writing (B1+)". |

No new dependencies were introduced; all pins remain as previously verified.
