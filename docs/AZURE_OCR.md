# Azure Document Intelligence OCR integration

## Current route

Mark My Words now uses one OCR provider for scanned documents:

```text
Native PDF text layer → Azure Document Intelligence Read (scanned PDF/JPG/PNG)
→ provisional rubric grading → teacher transcript review/score adjustment
→ teacher locks final grade
```

- A usable native PDF text layer remains the first route and is not sent to
  Azure.
- Azure Read is used only when a PDF has no usable text layer or when a JPG/PNG
  submission is uploaded.
- Azure returns an OCR candidate, not an authoritative transcript. The app
  requires a teacher to review and apply the transcript before locking an
  Azure-OCR grade.
- Gemini/Groq may still be used for rubric grading; neither is used as the
  scan/image OCR fallback in this route.

No second OCR provider or comparison route is included in this version.

## Azure server-side configuration

Set these in **Streamlit Community Cloud → App settings → Secrets**, not in the
repository and not in browser code:

```toml
AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT = "https://YOUR-RESOURCE.cognitiveservices.azure.com"
AZURE_DOCUMENT_INTELLIGENCE_KEY = "your-server-side-key"
AZURE_DOCUMENT_INTELLIGENCE_API_VERSION = "2024-11-30"
AZURE_DOCUMENT_INTELLIGENCE_LOCALE = "en"
AZURE_DOCUMENT_INTELLIGENCE_POLL_TIMEOUT_SECONDS = "120"
AZURE_DOCUMENT_INTELLIGENCE_POLL_INTERVAL_SECONDS = "1"
AZURE_DOCUMENT_INTELLIGENCE_PRICING_TIER = "F0" # F0 or S0
OCR_LOW_CONFIDENCE_THRESHOLD = "0.80"
OCR_REQUEST_TIMEOUT_SECONDS = "90"
```

Keep the endpoint and key in Streamlit secrets only. The adapter never sends a
filename or document URL to Azure, and no credential is returned to the
browser.

## F0/free-tier guard

The application defaults to `F0` unless `S0` is explicitly configured. In F0
mode it blocks a scan **before Azure is called** when it exceeds either of these
limits:

- 4 MB per PDF/JPG/PNG OCR request
- 2 PDF pages per OCR request

This matters because Azure F0 otherwise processes only the first two PDF pages;
the app refuses a larger scanned PDF and does not grade a sparse/partial PDF
text layer as a substitute. Native-text PDFs with a usable text layer may still
follow the local text-layer route under the normal application limits.

Use `S0` only after moving to a school-controlled Azure resource and obtaining
the required institutional approval for the intended student-data flow.

## Teacher review and locking

For Azure OCR results, the Batch Review screen now:

1. identifies Azure Read as the transcript source;
2. exposes the uncorrected Azure transcript for teacher editing;
3. highlights when Azure reported low-confidence words;
4. requires **Apply reviewed text** before **Lock Final Grade & Save to
   Database** becomes available; and
5. saves the reviewed text—not a raw provider response—with the locked record.

If a teacher changes the transcript materially, they should revise the rubric
sliders before locking the grade. Provider confidence is only a review cue; it
is not a score or correctness guarantee.

## Database migration before deployment

Run both migrations once in the project's Supabase SQL Editor before deploying
this version:

- `supabase/migrations/202608300001_azure_ocr_provenance.sql` — adds three
  non-content fields to `essay_memory` (`ocr_source`, `ocr_metadata`,
  `transcript_reviewed`) so a locked exemplar can retain the OCR route, safe
  review metadata, and transcript-review state. It also retires new writes to
  the unused legacy correction table without deleting historical rows.
- `supabase/migrations/202608300002_ensure_essay_memory_embedding.sql` —
  guarantees the calibration `embedding` column exists, documents its contract,
  and adds a GIN index. Idempotent, so it is safe on an already-migrated
  project.

Neither migration inserts a credential, raw Azure response, operation URL, or
document image.

## Privacy and operations

Before enabling this route for any student work, the institution should confirm
the approved Azure subscription/tenant, resource region, retention terms,
access controls, vendor agreement/DPA, incident process, and key rotation
policy. A personal free resource is operationally constrained and should not be
silently treated as an unrestricted school production service.

For F0 testing, use only samples permitted by the relevant school policy and
stay within the enforced limits. Do not paste keys into chat, commit them to
GitHub, or put them in a sample filename/document.

## Implementation notes

- `ocr_providers.py` contains the Azure Read REST adapter and performs local
  MIME-signature, size, F0 page-limit, endpoint-host, and redirect checks.
- Azure uses the `prebuilt-read` model and asynchronous polling. The adapter
  polls only an HTTPS operation URL on the configured Azure host, with redirects
  disabled.
- The app uses the existing 10 MB/30-page application bounds for S0 and stricter
  F0 limits where applicable. F0 analysis submissions are serial and spaced at
  least one second apart per app process.
- Tests use fake HTTP responses only; no real document or credential is used by
  the test suite.

## Official references

- [Azure Document Intelligence Read](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/prebuilt/read?view=doc-intel-4.0.0)
- [Azure Document Intelligence REST API](https://learn.microsoft.com/en-us/rest/api/aiservices/document-models/analyze-document?view=rest-aiservices-v4.0%20(2024-11-30))
- [Azure Document Intelligence service limits](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/service-limits?view=doc-intel-4.0.0)
