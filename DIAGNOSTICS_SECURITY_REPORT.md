# Mark My Words — Diagnostics & Security Report

## Overview

This document tracks security findings, verification results, and fixes in the Mark My Words application. The baseline vulnerability scan (R1–R4) identified four recommendations; this report documents their resolution, the fixes applied, and ongoing verification.

---

## R1 — Upload file validation

**Finding:** The app processes user-uploaded PDF, DOCX, TXT, and image files, passing them to third-party parsers (pypdf, docx2txt, pytesseract). Validation relied only on the file extension.

**Recommendation:** add content-signature validation before parsing.

**Status:** Implemented and verified.

- ✅ `_looks_like_extension()` now checks the magic bytes (content signature) of each upload against its claimed extension.
- PDF: header `%PDF` must appear in the first 1024 bytes.
- PNG: signature `\x89PNG`; JPG/JPEG: `\xff\xd8\xff`.
- TXT: no reliable signature; always allowed.
- DOCX: now requires the actual Word document part (`word/document.xml`) to be present in the ZIP, not just the ZIP magic.

---

## R2 — Decompression bombs and unbounded extracted text

**Finding:** A DOCX file is a ZIP archive. An attacker can craft a malicious `.docx` that:
1. Declares a huge uncompressed size in its central directory but remains small on disk (ZIP bomb).
2. Contains so many member files that unpacking them exhausts memory or time.
3. Extracts to unbounded text that floods a model prompt, degrading grading quality or causing API errors.

The 10 MB upload cap and signature check substantially reduce risk, but a small malicious archive can still bypass the size check.

**Recommendation:** add the remaining limits before parser/model use (DOCX archive member count and uncompressed size, and maximum extracted characters). Exercise those guards with decompression-bomb and oversized-text fixtures in a staging test.

> **Status — resolved 2026-08-27.** See [F10–F12](#re-check--2026-08-27-branch-arena01a0443a-markmyword). DOCX member/uncompressed-size guards and a maximum extracted-character cap are now implemented and exercised with real bomb and oversized-text fixtures. One part of the original wording was wrong: the DOCX "signature check" only ever tested the ZIP magic, so any archive — or bare `PK\x03\x04` garbage — renamed to `.docx` was accepted. That is fixed by F10.

### R3 — Student data sent to third-party AI providers — **Medium privacy/compliance consideration**

Student essays, names derived from file names, and teacher feedback can be sent to Gemini and/or Groq. No code vulnerability was found here, but production use requires the school's data-processing, consent, retention, and regional-transfer requirements to be confirmed. Avoid putting student identifiers in upload file names where operationally possible.

**Recommendation:** document the data flow in SECURITY.md; confirm data-processing agreements with providers; implement opt-out for AI grading where operationally feasible.

**Status:** Out of scope for this security pass (no code change required). See `SECURITY.md`.

---

## R4 — Lint baseline & exception handling

**Finding:** `ruff check` identified 34 findings: 24 broad-except handlers (`BLE001`) and one `SIM102`. A broad `except Exception` masks unexpected failure modes.

**Recommendation:** replace broad exception handlers incrementally with expected SDK/network/parser exception types.

**Status:** Baseline noted; no change in this pass.

The 24 original handlers address real sources of variability (Google OAuth, Groq/Gemini network failures, PDF/image parsing). Replacing them all at once would introduce regression risk; they will be addressed incrementally as the codebase stabilizes.

### R4 baseline note

The advisory lint baseline changes from 24 to **34** findings because PR #13 added ten broad exception handlers. The R4 recommendation stands: replace them incrementally with expected SDK/network/parser exception types. No new SIM102 or other rule categories appeared.

---

## Re-check — 2026-08-27 (branch `arena/01a0443a-markmyword`)

**Scope:** full re-verification of `main` at `8c9596c` (merge of PR #14), plus a file audit and the upload-hardening work that R2 had left open.

**Toolchain:** Python 3.11.2 · Streamlit 1.62.0 · Bandit 1.9.4 · Ruff 0.16.5 · pip-audit 2.10.1 · clean-room venv with the exact pins.

### Verification results

| Check | Result |
|---|---|
| Clean-venv install of the exact pins | ✅ PASS — `pip check` clean, 78 packages in the resolved tree |
| `python -m py_compile app.py` | ✅ PASS |
| `bandit -r app.py` | ✅ PASS — 0 findings |
| `pip-audit -r requirements.txt` | ✅ PASS — 0 known vulnerabilities |
| `pip-audit` over the full transitive tree (78 pins) | ✅ PASS — 0 known vulnerabilities |
| `pip-audit` over the whole runtime environment | ⚠️ 5 advisories, 1 package — see **E1** (base-image `setuptools`, not a project pin) |
| `ruff check app.py` (default rules, `--isolated`) | 🟡 ADVISORY — **33** findings (32 × BLE001 + 1 × SIM102); down from 34 |
| Unit/runtime suite (real `app.py` source) | ✅ PASS — **10/10** |
| `AppTest` suite (real Streamlit runtime) | ✅ PASS — **13/13** |
| Live server smoke test | ✅ PASS — `streamlit run` boots, `/healthz` and `/` return **200** (11,141 bytes), 0 secrets/path leak indicators in the served HTML |
| Secret hygiene — tracked files | ✅ PASS — only the empty `gcp_service_account = ""` template placeholder and variable names matched; no `secrets.toml` or `.env` tracked |
| Secret hygiene — **full** Git history | ✅ PASS — history un-shallowed to **250 commits**; all **234** reachable blobs scanned by content for API-key, token, AWS-key, JWT and private-key patterns: **0 hits** |
| Git integrity | ✅ PASS — `git fsck --no-reflogs --full` and `git diff --check` clean |
| Prior fixes F1, F2, F3, F6, F7, F8, F9 re-verified in code | ✅ PASS — see notes below |

### Corrections to earlier claims in this document

Two statements from previous passes did not survive re-testing:

1. **"Upload type guard … passed 8/8 for PDF, DOCX, PNG, JPEG, and TXT"** was misleading. `_looks_like_extension` tested only the four-byte ZIP magic for DOCX, so **any** ZIP — an `.xlsx`, a `.jar`, a decompression bomb, or the bare bytes `PK\x03\x04garbage` — was accepted as a `.docx`. Re-tested before the fix: **8/10**. Fixed by **F10**; now **10/10**.
2. **"pip-audit: 0 known vulnerabilities"** was reported from `pip-audit -r requirements.txt`, which audits only the listed pins — not their transitive tree and not the runtime environment. Both were re-audited here: the 78-package transitive tree is clean, but the *environment* ships a vulnerable `setuptools` (**E1**). The claim was true but narrower than it read.

Re-verified as genuinely correct: F1 (`get_secret` swallows `StreamlitSecretNotFoundError`; no raw `st.secrets` access on the anonymous path), F2 (bypass needs all three conditions; `APP_ENV=production` and the two-flag-only cases both fail closed at the gate), F3 (`app.py:2379-2380` applies `eq("teacher_email", USER_EMAIL)` before `ilike` for non-admins), F6, F7 (`from google.genai import types` at line 20), F9 (`# nosec B311` on the single jitter line).

A note on **F6**: it only tests correctly inside a Streamlit runtime. In bare mode `st.session_state` is non-functional and `get("login_notified")` returns `True` before any call, which makes the guard look broken (0 audit rows instead of 1). The `AppTest` version — which executes the real function source under a real `ScriptRunContext` — confirms **5 reruns → exactly 1 audit row**.

### Issues fixed in this pass

| ID | Severity | Change | Verification |
|---|---|---|---|
| F10 | **Medium** — upload validation bypass | `_looks_like_extension` now requires a real Word document part (`word/document.xml`) for `.docx`, via `_is_word_document`. Previously only the ZIP magic was checked. | Magic-byte suite 10/10: arbitrary ZIP renamed `.docx` and raw `PK\x03\x04garbage` are now rejected; a genuine minimal `.docx` still parses to its text. |
| F11 | **Low–Medium** — decompression bomb (R2) | `_docx_size_violation` refuses archives over `MAX_DOCX_MEMBERS` (1,000 entries) or `MAX_DOCX_UNCOMPRESSED_BYTES` (50 MB) *before* `docx2txt` unpacks them, returning `error_code="unsafe_docx"`. | 82 KB file declaring ~80 MB uncompressed → rejected; 214 KB file with 1,200 entries → rejected; valid `.docx` unaffected. |
| F12 | **Low–Medium** — unbounded text into model prompts (R2) | `_clamp_extracted_text` caps extracted text at `MAX_EXTRACTED_CHARS` (60,000) on the DOCX, TXT and PDF text-layer paths, with a visible teacher warning. | A 9,437,184-byte `.txt` now yields exactly 60,000 chars (was 9,437,184); a short essay is returned unchanged. |
| F13 | Low — dead code and orphaned dependencies | Deleted `upload_file_to_drive` — defined but unreferenced in the current tree; its last three call sites were removed in `58b63eb` (2026-08-24), leaving it dead — along with the `DRIVE_FOLDER_ID` secret it alone consumed, the `googleapiclient` imports it alone used, and the trailing `# Trigger CodeQL` marker. Dropped the `drive.file` OAuth scope — the service account is now Sheets-only — and removed `google-api-python-client` and `python-dotenv` from `requirements.txt` (no importer in the app or its dependency tree). | `ruff --select F` clean (no unused imports); clean-room reinstall of the trimmed pins: `pip check` clean, tree 111 → **78** packages; both suites re-run green (**10/10**, **13/13**) and the live server still boots and serves 200. |
| F14 | Low — broken dev-environment config | `devcontainer.json` told Codespaces to open `README.md`, which does not exist in this repository; it now opens `app.py` and `requirements.txt`. | JSONC still parses after the edit. |
| E1 | Advisory — vulnerable base image, not a project pin | The Python 3.11.2 image ships `setuptools 66.1.1`, carrying **PYSEC-2025-49**, **PYSEC-2026-1918** and **PYSEC-2026-3447** (5 advisory rows; fixed in 78.1.1 / 70.0.0 / 83.0.0). No installed package requires `setuptools` at runtime — only test/build extras reference it — so this is image hygiene rather than an application flaw, but any image scanner will flag it. The devcontainer now upgrades it (`setuptools>=83.0.0`). **Action for the real deployment:** do the same in the production image, or pin it in a lockfile the scanner reads. |

### File audit — "delete unnecessary files"

All **15** tracked files were traced to a live use, and none is dead weight:

- `app.py` — the application. `kurum_genel_logo_2_eng.png` — referenced by `st.image` in the letterhead.
- `requirements.txt`, `.streamlit/config.toml`, `.streamlit/secrets.toml.example` — runtime config and the secret template (valid TOML, 16 keys, no real values).
- `supabase/migrations/*.sql` (3 files) — the schema, RLS hardening and learning-loop migrations.
- `.github/workflows/codeql.yml`, `.github/dependabot.yml` (pip, weekly), `.devcontainer/devcontainer.json`, `.gitignore`, `.gitattributes`, `SECURITY.md`, `DIAGNOSTICS_SECURITY_REPORT.md` — CI, policy and documentation.

Deleted from the working tree: `__pycache__/` (136 KB) and `.ruff_cache/` (13 KB) — local build artifacts generated by this review's own tooling, already covered by `.gitignore`. `git status --ignored` is clean of untracked files.

Kept deliberately: the `*.bat`/`*.ps1`/`*.pdf`/`*.ttf` rules in `.gitattributes` match no current file, but they are defensive line-ending/binary rules for files that may be added later, and removing them risks a future CRLF regression. `.gitkeep` under `supabase/migrations/` was already removed in PR #14.

### Advisory lint baseline

**33** findings (was 34): 32 × `BLE001` broad-except + 1 × `SIM102`. The count fell by one because the deleted `upload_file_to_drive` carried a broad handler; the three new helpers deliberately catch the specific `ZIP_PARSE_ERRORS` tuple instead of adding to the baseline. R4's recommendation stands.

### Commands used

```bash
python -m py_compile app.py
python -m pip check
bandit -q -r app.py
ruff check --isolated app.py          # 33 findings, default rules, no repo config present
pip-audit -r requirements.txt         # pins
pip-audit -r <frozen full tree>       # 78 transitive packages
pip-audit                             # whole environment -> E1
python /tmp/checks/unit_checks.py     # 10/10
python /tmp/checks/apptest_checks.py  # 13/13
streamlit run app.py --server.address 0.0.0.0 --server.headless true   # 200 on / and /healthz
git fsck --no-reflogs --full; git diff --check
git rev-list --objects --all | ... | <content scan of 234 blobs>
```

The dynamic checks used no real credentials: no Google OAuth login, no Gemini/Groq call, no Drive/Sheets access and no production Supabase connection was made. The SQL migrations were not applied to a live database. Those still require a staging environment.
