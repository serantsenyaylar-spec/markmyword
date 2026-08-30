# GitHub + Streamlit Community Cloud deployment: Azure Read OCR

This guide deploys the Azure-only OCR refactor without creating a local secrets
file or exposing an endpoint/key.

## Before you start

- Keep the Azure endpoint/key only in **Streamlit Community Cloud → App
  settings → Secrets**.
- Do not paste either value into GitHub, this repository, an issue, or chat.
- The code defaults to `F0` if the tier setting is omitted, but explicitly
  setting it to `F0` makes the intended limit visible in the Cloud secrets UI.

## Step 1 — Apply the Supabase migration first

1. Open the Supabase project used by Mark My Words.
2. Open **SQL Editor** and start a new query.
3. Open the file
   `supabase/migrations/202608300001_azure_ocr_provenance.sql` from this
   release bundle, copy its full contents into the query, and click **Run**.
4. Confirm that it completes successfully before deploying the app.

It adds `ocr_source`, `ocr_metadata`, and `transcript_reviewed` to
`essay_memory`, and retires new authenticated writes to the unused legacy
correction table without deleting historical rows. It does not send data to
Azure and does not contain a secret.

## Step 2 — Verify Streamlit Community Cloud Secrets

In the existing app's **App settings → Secrets**, retain your existing OAuth,
grading, Sheets/Drive, and Supabase settings. Confirm these Azure entries are
present, without sharing their values:

```toml
AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT = "..."
AZURE_DOCUMENT_INTELLIGENCE_KEY = "..."
AZURE_DOCUMENT_INTELLIGENCE_PRICING_TIER = "F0"
```

Optional safe defaults, if they are not already present:

```toml
AZURE_DOCUMENT_INTELLIGENCE_API_VERSION = "2024-11-30"
AZURE_DOCUMENT_INTELLIGENCE_LOCALE = "en"
AZURE_DOCUMENT_INTELLIGENCE_POLL_TIMEOUT_SECONDS = "120"
AZURE_DOCUMENT_INTELLIGENCE_POLL_INTERVAL_SECONDS = "1"
OCR_LOW_CONFIDENCE_THRESHOLD = "0.80"
OCR_REQUEST_TIMEOUT_SECONDS = "90"
```

Do not add `APP_ENV`, `ALLOW_DEV_BYPASS`, or `DEV_AUTH_BYPASS` to hosted Cloud
Secrets.

## Step 3 — Commit the release bundle to GitHub

Use a new branch first, for example `azure-read-production`.

The release archive contains the complete working tree. Extract it, then add
its contents (not the ZIP itself) to the GitHub repository while preserving the
folders beginning with a dot, especially `.github/` and `.streamlit/`. Commit
all files together with a message such as:

```text
Use Azure Document Intelligence Read for scanned submissions
```

Important checks before merging the branch:

- `ocr_providers.py` is present at repository root.
- `docs/AZURE_OCR.md`, `docs/GITHUB_STREAMLIT_DEPLOY.md`, and `tests/` are
  present.
- `supabase/migrations/202608300001_azure_ocr_provenance.sql` is present.
- `.streamlit/secrets.toml` is **not** present.
- No old comparison archive, OCR benchmark script, or second OCR-provider
  configuration is uploaded.

GitHub Actions should run the offline Azure OCR tests and dependency audit.
Wait for the CI workflow to pass before merging to `main`.

## Step 4 — Let Streamlit redeploy

Once `main` has the change, Streamlit Community Cloud will normally redeploy
from GitHub. In the Streamlit dashboard, open the app logs and confirm the
new deployment starts without a missing-module error. Do not use a green
configuration card as proof of Azure authentication or OCR quality.

## Step 5 — First approved live smoke test

Use one institution-approved, non-sensitive/de-identified, one-page scan or
photo under 4 MB. The first live test should verify all of the following:

1. The upload caption identifies **Azure Document Intelligence — Read**.
2. The raw Azure transcript appears in Batch Review.
3. Low-confidence words, if Azure reports any, are shown as a review cue.
4. Editing/confirming the transcript requires **Apply reviewed text** before
   the final-grade lock button becomes available.
5. A 3-page scanned PDF and a scan over 4 MB are rejected locally in F0 mode;
   they are not partially graded or sent to Azure.

Also upload one ordinary native-text PDF to confirm it follows the local
text-layer route. Do not use a real student document for the first test unless
that exact use is within the school's approval.

## What this release changes

- Uses Azure Document Intelligence Read for scanned PDFs and JPG/PNG uploads.
- Keeps native PDF text-layer extraction first; DOCX/TXT behavior is unchanged.
- Leaves Gemini/Groq available for grading only.
- Enforces F0 locally: 4 MB input maximum, two scanned-PDF pages maximum, and
  one Azure F0 analysis submission per second per app process.
- Preserves Azure transcript content without silent correction and requires a
  teacher review before grade locking.
- Stores only safe OCR provenance with the locked exemplar; no raw Azure
  response, operation URL, endpoint, or key is persisted.

All automated checks in this release use fake HTTP responses. No live Azure
request, secret value, or student document was used while preparing it.
