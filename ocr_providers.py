"""Privacy-conscious OCR adapters for Mark My Words benchmark work.

This module deliberately uses the vendors' HTTPS APIs rather than adding large
provider SDKs.  It has no Streamlit dependency, so the same adapters can be
used by the admin benchmark screen, a controlled command-line benchmark, and
unit tests.

Important scope
---------------
The adapters are *not* wired into the normal student-grading upload route.
They are intended for the initial, de-identified comparison of Google Document
AI Enterprise OCR and Azure Document Intelligence Read.  A school privacy,
contractual, and operational approval is required before either becomes a
production OCR path.

No input bytes, raw provider response, credentials, or document filenames are
written to logs or returned in the result metadata.  The caller receives only
the OCR transcript and concise review metadata.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

GOOGLE_DOCUMENT_AI = "google_document_ai"
AZURE_DOCUMENT_INTELLIGENCE = "azure_document_intelligence"

PROVIDER_LABELS = {
    GOOGLE_DOCUMENT_AI: "Google Document AI — Enterprise OCR",
    AZURE_DOCUMENT_INTELLIGENCE: "Azure Document Intelligence — Read",
}

# The benchmark accepts the overlap of the file types supported by the two
# providers and by Mark My Words' existing upload UI.  Keeping the comparison
# to a common input set makes results meaningful.
SUPPORTED_MIME_TYPES = frozenset({"application/pdf", "image/jpeg", "image/png"})
# Match the existing Mark My Words upload guard. This bound is enforced again
# inside the adapter so a future caller cannot accidentally bypass the UI cap.
MAX_OCR_INPUT_BYTES = 10 * 1024 * 1024

# A score below this value is shown to the teacher as an item to verify.  It is
# a provider model-confidence threshold, not a claim about factual accuracy or
# a grading threshold.
DEFAULT_LOW_CONFIDENCE_THRESHOLD = 0.80


class OCRProviderError(RuntimeError):
    """A safe, displayable OCR-provider failure.

    ``status_code`` and ``retry_after_seconds`` let a caller apply its own
    retry policy without parsing vendor-specific prose.  Error messages are
    deliberately bounded and never include request bodies or credentials.
    """

    def __init__(
        self,
        message: str,
        *,
        provider_id: str = "",
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class OCRNotConfiguredError(OCRProviderError):
    """Raised when a selected provider lacks one or more server-side secrets."""


class OCRPolicyError(OCRProviderError):
    """Raised when a caller asks the benchmark adapter to handle an unsafe input."""


@dataclass(frozen=True)
class OCRWord:
    """One low-confidence OCR token that a teacher should check against the scan."""

    text: str
    confidence: float
    page_number: int | None = None


@dataclass(frozen=True)
class OCRResult:
    """Vendor-neutral OCR output suitable for teacher review and benchmarking."""

    transcript: str
    provider_id: str
    provider_label: str
    page_count: int | None
    confidence: Mapping[str, Any] | None = None
    handwriting_detected: bool | None = None
    low_confidence_words: tuple[OCRWord, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def review_metadata(self) -> dict[str, Any]:
        """Return non-transcript metadata safe to attach to a UI result.

        The OCR transcript stays separate so downstream code cannot accidentally
        serialize an entire vendor response when it only needs provenance.
        """

        output: dict[str, Any] = {
            "source": "ocr",
            "provider_id": self.provider_id,
            "provider": self.provider_label,
            "page_count": self.page_count,
            "handwriting_detected": self.handwriting_detected,
        }
        if self.confidence:
            output["confidence"] = dict(self.confidence)
        if self.metadata:
            output.update(dict(self.metadata))
        return output


@dataclass(frozen=True)
class TranscriptComparison:
    """Exact-token transcript comparison against a teacher-verified reference."""

    reference_words: int
    transcript_words: int
    word_edits: int
    word_error_rate: float | None
    character_edits: int
    character_error_rate: float | None


# ---------------------------------------------------------------------------
# Generic configuration, validation, and result helpers
# ---------------------------------------------------------------------------
def canonical_provider_id(provider_id: str) -> str:
    """Normalize friendly provider aliases to the two benchmark provider IDs."""

    normalized = str(provider_id or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "google": GOOGLE_DOCUMENT_AI,
        "document_ai": GOOGLE_DOCUMENT_AI,
        "google_document_ai_enterprise": GOOGLE_DOCUMENT_AI,
        "google_document_ai_enterprise_ocr": GOOGLE_DOCUMENT_AI,
        "azure": AZURE_DOCUMENT_INTELLIGENCE,
        "azure_document_intelligence_read": AZURE_DOCUMENT_INTELLIGENCE,
        "azure_read": AZURE_DOCUMENT_INTELLIGENCE,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in PROVIDER_LABELS:
        options = ", ".join(PROVIDER_LABELS)
        raise OCRPolicyError(f"Unknown OCR provider {provider_id!r}. Choose one of: {options}.")
    return normalized


def provider_label(provider_id: str) -> str:
    """Return the human-friendly provider name for a canonical or alias ID."""

    return PROVIDER_LABELS[canonical_provider_id(provider_id)]


def _value(config: Mapping[str, Any], key: str, default: Any = "") -> Any:
    value = config.get(key, default)
    if value is None:
        return default
    return value


def _nonempty(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def missing_configuration(provider_id: str, config: Mapping[str, Any]) -> list[str]:
    """Return missing secret names without exposing their values."""

    canonical = canonical_provider_id(provider_id)
    if canonical == GOOGLE_DOCUMENT_AI:
        required = (
            "GOOGLE_DOCUMENT_AI_PROJECT_ID",
            "GOOGLE_DOCUMENT_AI_LOCATION",
            "GOOGLE_DOCUMENT_AI_PROCESSOR_ID",
            "GOOGLE_DOCUMENT_AI_SERVICE_ACCOUNT_JSON",
        )
    else:
        required = (
            "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
            "AZURE_DOCUMENT_INTELLIGENCE_KEY",
        )
    return [key for key in required if not _nonempty(_value(config, key))]


def configured_providers(config: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the benchmark providers whose required secrets are present."""

    return tuple(
        provider
        for provider in PROVIDER_LABELS
        if not missing_configuration(provider, config)
    )


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _bounded_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    parsed = _as_float(value)
    if parsed is None:
        return default
    return max(minimum, min(maximum, parsed))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _confidence_summary(
    values: list[float],
    *,
    unit: str,
    threshold: float,
) -> dict[str, Any] | None:
    """Summarize vendor confidence scores without pretending they are accuracy."""

    valid = [score for score in values if 0.0 <= score <= 1.0]
    if not valid:
        return None
    low_count = sum(score < threshold for score in valid)
    return {
        "unit": unit,
        "count": len(valid),
        "mean": round(sum(valid) / len(valid), 4),
        "minimum": round(min(valid), 4),
        "maximum": round(max(valid), 4),
        "low_confidence_threshold": round(threshold, 4),
        "low_confidence_count": low_count,
        "low_confidence_rate": round(low_count / len(valid), 4),
    }


def _sorted_low_confidence_words(words: list[OCRWord], threshold: float) -> tuple[OCRWord, ...]:
    """Keep a bounded, deterministic review queue rather than every OCR token."""

    relevant = [word for word in words if word.confidence < threshold and word.text]
    relevant.sort(key=lambda word: (word.confidence, word.page_number or 0, word.text.casefold()))
    return tuple(relevant[:50])


def _response_status_code(response: Any) -> int | None:
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    return None


def _response_headers(response: Any) -> Mapping[str, Any]:
    headers = getattr(response, "headers", {})
    return headers if isinstance(headers, Mapping) else {}


def _header(headers: Mapping[str, Any], name: str) -> str:
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            return str(value)
    return ""


def _retry_after_seconds(response: Any) -> float | None:
    value = _as_float(_header(_response_headers(response), "Retry-After"))
    return value if value is not None and value >= 0 else None


def _safe_provider_error_text(response: Any) -> str:
    """Extract a short vendor error description while avoiding request/body leaks."""

    code = ""
    message = ""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - response parser is provider controlled
        body = None
    if isinstance(body, Mapping):
        error = body.get("error", body)
        if isinstance(error, Mapping):
            code = str(error.get("code") or "").strip()
            message = str(error.get("message") or "").strip()
    if not message:
        message = "The provider rejected the OCR request."
    message = " ".join(message.split())[:240]
    return f"{code}: {message}".strip(": ")


def _raise_for_unsuccessful_response(response: Any, provider_id: str, action: str) -> None:
    status = _response_status_code(response)
    if status is not None and 200 <= status < 300:
        return
    status_display = str(status) if status is not None else "unknown HTTP status"
    detail = _safe_provider_error_text(response)
    raise OCRProviderError(
        f"{provider_label(provider_id)} {action} failed ({status_display}): {detail}",
        provider_id=provider_id,
        status_code=status,
        retry_after_seconds=_retry_after_seconds(response),
    )


def _response_json(response: Any, provider_id: str, action: str) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - present a provider-safe error to the caller
        raise OCRProviderError(
            f"{provider_label(provider_id)} {action} returned invalid JSON.",
            provider_id=provider_id,
            status_code=_response_status_code(response),
        ) from exc
    if not isinstance(payload, Mapping):
        raise OCRProviderError(
            f"{provider_label(provider_id)} {action} returned an unexpected response.",
            provider_id=provider_id,
            status_code=_response_status_code(response),
        )
    return payload


def _requests_session() -> Any:
    """Import requests only at call time, keeping parser unit tests dependency-free."""

    try:
        import requests
    except ImportError as exc:  # pragma: no cover - CI installs requirements.txt
        raise OCRProviderError(
            "The requests package is not installed; install requirements.txt before using cloud OCR."
        ) from exc
    return requests.Session()


def validate_ocr_input(file_bytes: bytes, mime_type: str) -> None:
    """Reject unsupported, oversized, or mislabeled inputs before authentication or HTTP."""

    if not isinstance(file_bytes, (bytes, bytearray)) or not file_bytes:
        raise OCRPolicyError("OCR needs a non-empty PDF, JPG, or PNG file.")
    if len(file_bytes) > MAX_OCR_INPUT_BYTES:
        raise OCRPolicyError(
            f"OCR input is over the {MAX_OCR_INPUT_BYTES // (1024 * 1024)} MB benchmark safety limit."
        )
    if mime_type not in SUPPORTED_MIME_TYPES:
        supported = ", ".join(sorted(SUPPORTED_MIME_TYPES))
        raise OCRPolicyError(f"This benchmark supports only: {supported}.")

    # Never send an obviously mislabeled upload to either cloud endpoint. PDFs
    # can place the header after a small byte-order/comment prefix, so match the
    # existing app's first-1024-bytes rule.
    content = bytes(file_bytes)
    is_valid = {
        "application/pdf": b"%PDF-" in content[:1024],
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
    }[mime_type]
    if not is_valid:
        raise OCRPolicyError("The OCR file bytes do not match the claimed PDF, JPG, or PNG type.")


def _provider_text(text: Any) -> str:
    """Return provider-supplied text unchanged; never silently alter an OCR candidate."""

    return text if isinstance(text, str) else ""


def _join_page_lines(page_lines: list[list[str]], fallback_text: str) -> str:
    """Build a readable fallback only when a provider omitted its full transcript."""

    pages = ["\n".join(lines) for lines in page_lines]
    text = "\n\n".join(page for page in pages if page)
    return text or fallback_text


def _edit_distance(left: list[str], right: list[str]) -> int:
    """Memory-efficient Levenshtein distance for benchmark WER/CER."""

    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for i, source_item in enumerate(left, start=1):
        current = [i]
        for j, target_item in enumerate(right, start=1):
            insertion = current[j - 1] + 1
            deletion = previous[j] + 1
            substitution = previous[j - 1] + (source_item != target_item)
            current.append(min(insertion, deletion, substitution))
        previous = current
    return previous[-1]


def compare_transcript(reference: str, transcript: str) -> TranscriptComparison:
    """Compare verbatim OCR against teacher text with whitespace-only normalization.

    Case, punctuation, spelling, and words are intentionally *not* normalized:
    the goal is to detect transcription changes that could affect grading.
    """

    reference_words = str(reference or "").split()
    transcript_words = str(transcript or "").split()
    word_edits = _edit_distance(reference_words, transcript_words)
    reference_characters = list(" ".join(reference_words))
    transcript_characters = list(" ".join(transcript_words))
    character_edits = _edit_distance(reference_characters, transcript_characters)
    return TranscriptComparison(
        reference_words=len(reference_words),
        transcript_words=len(transcript_words),
        word_edits=word_edits,
        word_error_rate=(word_edits / len(reference_words)) if reference_words else None,
        character_edits=character_edits,
        character_error_rate=(character_edits / len(reference_characters)) if reference_characters else None,
    )


# ---------------------------------------------------------------------------
# Google Document AI Enterprise OCR
# ---------------------------------------------------------------------------
def _google_service_account_info(config: Mapping[str, Any]) -> dict[str, Any]:
    raw = _value(config, "GOOGLE_DOCUMENT_AI_SERVICE_ACCOUNT_JSON")
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        raise OCRNotConfiguredError(
            "Google Document AI needs GOOGLE_DOCUMENT_AI_SERVICE_ACCOUNT_JSON.",
            provider_id=GOOGLE_DOCUMENT_AI,
        )
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OCRNotConfiguredError(
            "GOOGLE_DOCUMENT_AI_SERVICE_ACCOUNT_JSON must contain valid JSON.",
            provider_id=GOOGLE_DOCUMENT_AI,
        ) from exc
    if not isinstance(decoded, dict):
        raise OCRNotConfiguredError(
            "GOOGLE_DOCUMENT_AI_SERVICE_ACCOUNT_JSON must be a service-account JSON object.",
            provider_id=GOOGLE_DOCUMENT_AI,
        )
    return decoded


def _google_access_token(config: Mapping[str, Any]) -> str:
    """Mint a short-lived OAuth token from the dedicated Document AI identity."""

    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover - requirements install this in production
        raise OCRProviderError(
            "google-auth is not installed; install requirements.txt before using Google Document AI.",
            provider_id=GOOGLE_DOCUMENT_AI,
        ) from exc

    try:
        credentials = service_account.Credentials.from_service_account_info(
            _google_service_account_info(config),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        credentials.refresh(Request())
    except OCRProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 - do not leak key material from auth errors
        raise OCRProviderError(
            "Google Document AI could not obtain a service-account access token. "
            "Check the dedicated service account, key, and IAM role.",
            provider_id=GOOGLE_DOCUMENT_AI,
        ) from exc

    if not credentials.token:
        raise OCRProviderError(
            "Google Document AI did not receive an access token from the service account.",
            provider_id=GOOGLE_DOCUMENT_AI,
        )
    return credentials.token


def _safe_resource_component(value: Any, setting_name: str) -> str:
    text = str(value or "").strip()
    if not text or "/" in text or "\\" in text:
        raise OCRNotConfiguredError(
            f"{setting_name} must be a single non-empty resource identifier.",
            provider_id=GOOGLE_DOCUMENT_AI,
        )
    return quote(text, safe="-_.~")


def _google_processor_name(config: Mapping[str, Any]) -> tuple[str, str]:
    missing = missing_configuration(GOOGLE_DOCUMENT_AI, config)
    if missing:
        raise OCRNotConfiguredError(
            "Google Document AI is not configured: missing " + ", ".join(missing) + ".",
            provider_id=GOOGLE_DOCUMENT_AI,
        )

    project_id = _safe_resource_component(_value(config, "GOOGLE_DOCUMENT_AI_PROJECT_ID"), "GOOGLE_DOCUMENT_AI_PROJECT_ID")
    location = _safe_resource_component(_value(config, "GOOGLE_DOCUMENT_AI_LOCATION"), "GOOGLE_DOCUMENT_AI_LOCATION")
    processor_id = _safe_resource_component(
        _value(config, "GOOGLE_DOCUMENT_AI_PROCESSOR_ID"),
        "GOOGLE_DOCUMENT_AI_PROCESSOR_ID",
    )
    processor_version = str(_value(config, "GOOGLE_DOCUMENT_AI_PROCESSOR_VERSION") or "").strip()

    name = f"projects/{project_id}/locations/{location}/processors/{processor_id}"
    if processor_version:
        name += "/processorVersions/" + _safe_resource_component(
            processor_version,
            "GOOGLE_DOCUMENT_AI_PROCESSOR_VERSION",
        )
    return name, location


def _google_ocr_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Build only documented Enterprise OCR options needed for the benchmark."""

    ocr_config: dict[str, Any] = {
        # This does not replace Mark My Words' local text-layer-first check; it
        # simply gives Document AI the best documented behavior if a benchmark
        # sample happens to contain a native text layer.
        "enableNativePdfParsing": True,
        "enableImageQualityScores": _as_bool(
            _value(config, "GOOGLE_DOCUMENT_AI_ENABLE_IMAGE_QUALITY_SCORES", "true"),
            default=True,
        ),
    }
    language_hints = _value(config, "GOOGLE_DOCUMENT_AI_LANGUAGE_HINTS")
    if isinstance(language_hints, str):
        hints = [hint.strip() for hint in language_hints.split(",") if hint.strip()]
    elif isinstance(language_hints, (list, tuple)):
        hints = [str(hint).strip() for hint in language_hints if str(hint).strip()]
    else:
        hints = []
    if hints:
        ocr_config["hints"] = {"languageHints": hints}
    return ocr_config


def _text_from_google_anchor(anchor: Any, document_text: str) -> str:
    if not isinstance(anchor, Mapping):
        return ""
    # Some Document AI objects expose content directly; use it when present to
    # avoid assumptions about offset representation.
    direct = anchor.get("content")
    if isinstance(direct, str) and direct:
        return direct

    parts: list[str] = []
    segments = anchor.get("textSegments") or []
    if not isinstance(segments, list):
        return ""
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        start_raw = segment.get("startIndex", 0)
        end_raw = segment.get("endIndex")
        try:
            start = int(start_raw or 0)
            end = int(end_raw)
        except (TypeError, ValueError):
            continue
        if 0 <= start <= end <= len(document_text):
            parts.append(document_text[start:end])
    return "".join(parts)


def _google_quality_scores(page: Mapping[str, Any]) -> list[float]:
    raw = page.get("imageQualityScores")
    candidates = raw if isinstance(raw, list) else [raw]
    scores: list[float] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        score = _as_float(candidate.get("qualityScore"))
        if score is not None and 0.0 <= score <= 1.0:
            scores.append(score)
    return scores


def parse_google_document_ai_response(
    payload: Mapping[str, Any],
    *,
    low_confidence_threshold: float = DEFAULT_LOW_CONFIDENCE_THRESHOLD,
) -> OCRResult:
    """Translate a Document AI ProcessResponse into a verbatim review result."""

    document = payload.get("document")
    if not isinstance(document, Mapping):
        raise OCRProviderError(
            "Google Document AI returned no document OCR result.",
            provider_id=GOOGLE_DOCUMENT_AI,
        )
    document_error = document.get("error")
    if isinstance(document_error, Mapping) and document_error.get("code") not in (None, 0, "0"):
        message = " ".join(str(document_error.get("message") or "OCR processing failed.").split())[:240]
        raise OCRProviderError(message, provider_id=GOOGLE_DOCUMENT_AI)

    source_text = _provider_text(document.get("text"))
    pages = document.get("pages") or []
    if not isinstance(pages, list):
        pages = []

    page_lines: list[list[str]] = []
    token_scores: list[float] = []
    token_words: list[OCRWord] = []
    line_scores: list[float] = []
    line_words: list[OCRWord] = []
    handwriting_flags: list[bool] = []
    quality_scores: list[float] = []

    for index, raw_page in enumerate(pages, start=1):
        page = raw_page if isinstance(raw_page, Mapping) else {}
        page_number = int(_as_float(page.get("pageNumber")) or index)
        lines: list[str] = []
        raw_lines = page.get("lines") or []
        if isinstance(raw_lines, list):
            for raw_line in raw_lines:
                if not isinstance(raw_line, Mapping):
                    continue
                layout = raw_line.get("layout")
                if not isinstance(layout, Mapping):
                    continue
                line = _provider_text(_text_from_google_anchor(layout.get("textAnchor"), source_text))
                if line:
                    lines.append(line)
                score = _as_float(layout.get("confidence"))
                if score is not None and 0.0 <= score <= 1.0:
                    line_scores.append(score)
                    line_words.append(OCRWord(line, score, page_number))
        page_lines.append(lines)
        quality_scores.extend(_google_quality_scores(page))

        # Token confidence is the smallest generally available Document AI OCR
        # unit.  The line values above remain a separate fallback so a summary
        # never misleadingly mixes token and line confidence scores.
        raw_tokens = page.get("tokens") or []
        if not isinstance(raw_tokens, list):
            continue
        for raw_token in raw_tokens:
            if not isinstance(raw_token, Mapping):
                continue
            layout = raw_token.get("layout")
            if not isinstance(layout, Mapping):
                continue
            score = _as_float(layout.get("confidence"))
            if score is not None and 0.0 <= score <= 1.0:
                token_scores.append(score)
                token_text = _provider_text(_text_from_google_anchor(layout.get("textAnchor"), source_text))
                token_words.append(OCRWord(token_text, score, page_number))
            style_info = raw_token.get("styleInfo")
            if isinstance(style_info, Mapping) and isinstance(style_info.get("handwritten"), bool):
                handwriting_flags.append(style_info["handwritten"])

    threshold = _bounded_float(
        low_confidence_threshold,
        DEFAULT_LOW_CONFIDENCE_THRESHOLD,
        minimum=0.0,
        maximum=1.0,
    )
    if token_scores:
        confidence = _confidence_summary(token_scores, unit="token", threshold=threshold)
        low_confidence_words = _sorted_low_confidence_words(token_words, threshold)
    else:
        confidence = _confidence_summary(line_scores, unit="line", threshold=threshold)
        low_confidence_words = _sorted_low_confidence_words(line_words, threshold)
    metadata: dict[str, Any] = {
        "processor_version": "response/default",
    }
    if quality_scores:
        metadata["image_quality_score"] = {
            "count": len(quality_scores),
            "mean": round(sum(quality_scores) / len(quality_scores), 4),
            "minimum": round(min(quality_scores), 4),
        }

    return OCRResult(
        # Document AI's full document text is the authoritative vendor output.
        # Lines are used only as a fallback for unusual responses that omit it.
        transcript=source_text or _join_page_lines(page_lines, source_text),
        provider_id=GOOGLE_DOCUMENT_AI,
        provider_label=PROVIDER_LABELS[GOOGLE_DOCUMENT_AI],
        page_count=len(pages) or None,
        confidence=confidence,
        # Style information is optional; report handwriting only when the
        # provider actually returned a token-level handwritten flag.
        handwriting_detected=(any(handwriting_flags) if handwriting_flags else None),
        low_confidence_words=low_confidence_words,
        metadata=metadata,
    )


def process_google_document_ai(
    file_bytes: bytes,
    mime_type: str,
    config: Mapping[str, Any],
    *,
    session: Any | None = None,
    access_token_getter: Callable[[Mapping[str, Any]], str] | None = None,
) -> OCRResult:
    """Process one PDF/JPG/PNG through Enterprise Document OCR synchronously."""

    validate_ocr_input(file_bytes, mime_type)
    processor_name, location = _google_processor_name(config)
    token = (access_token_getter or _google_access_token)(config)
    if not isinstance(token, str) or not token.strip():
        raise OCRProviderError(
            "Google Document AI did not receive a usable access token.",
            provider_id=GOOGLE_DOCUMENT_AI,
        )

    endpoint = f"https://{location}-documentai.googleapis.com"
    request_url = f"{endpoint}/v1/{processor_name}:process"
    payload = {
        "skipHumanReview": True,
        # Avoid receiving image content in the API response when only text and
        # confidence metadata are needed for the benchmark.
        "imagelessMode": True,
        "rawDocument": {
            "mimeType": mime_type,
            "content": base64.b64encode(bytes(file_bytes)).decode("ascii"),
        },
        "processOptions": {"ocrConfig": _google_ocr_config(config)},
    }
    http = session or _requests_session()
    try:
        response = http.post(
            request_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
            allow_redirects=False,
            timeout=_bounded_float(
                _value(config, "OCR_REQUEST_TIMEOUT_SECONDS", 90),
                90.0,
                minimum=5.0,
                maximum=300.0,
            ),
        )
    except OCRProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 - callers apply their transient retry policy
        raise OCRProviderError(
            "Google Document AI request could not reach the service.",
            provider_id=GOOGLE_DOCUMENT_AI,
        ) from exc

    _raise_for_unsuccessful_response(response, GOOGLE_DOCUMENT_AI, "processing request")
    result = parse_google_document_ai_response(
        _response_json(response, GOOGLE_DOCUMENT_AI, "processing request"),
        low_confidence_threshold=_bounded_float(
            _value(config, "OCR_LOW_CONFIDENCE_THRESHOLD", DEFAULT_LOW_CONFIDENCE_THRESHOLD),
            DEFAULT_LOW_CONFIDENCE_THRESHOLD,
            minimum=0.0,
            maximum=1.0,
        ),
    )
    metadata = dict(result.metadata or {})
    # Preserve the configured processor version (if any) without exposing a
    # project, processor, or credential identifier in teacher-visible output.
    metadata["processor_version"] = str(_value(config, "GOOGLE_DOCUMENT_AI_PROCESSOR_VERSION") or "default")
    return OCRResult(
        transcript=result.transcript,
        provider_id=result.provider_id,
        provider_label=result.provider_label,
        page_count=result.page_count,
        confidence=result.confidence,
        handwriting_detected=result.handwriting_detected,
        low_confidence_words=result.low_confidence_words,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Azure Document Intelligence Read
# ---------------------------------------------------------------------------
def _validated_azure_endpoint(value: Any) -> str:
    endpoint = str(value or "").strip().rstrip("/")
    parsed = urlsplit(endpoint)
    try:
        parsed.port  # Validate a configured explicit port before any request.
    except ValueError:
        raise OCRNotConfiguredError(
            "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT must use a valid HTTPS port.",
            provider_id=AZURE_DOCUMENT_INTELLIGENCE,
        ) from None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise OCRNotConfiguredError(
            "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT must be an HTTPS resource endpoint without a path.",
            provider_id=AZURE_DOCUMENT_INTELLIGENCE,
        )
    return endpoint


def _azure_operation_url_is_safe(operation_url: str, endpoint: str) -> bool:
    """Avoid following an unexpected host supplied in a response header."""

    try:
        operation = urlsplit(operation_url)
        configured = urlsplit(endpoint)
        operation_port = operation.port or 443
        configured_port = configured.port or 443
    except ValueError:
        return False
    return (
        operation.scheme == "https"
        and not operation.username
        and not operation.password
        and operation.hostname == configured.hostname
        and operation_port == configured_port
    )


def parse_azure_document_intelligence_response(
    payload: Mapping[str, Any],
    *,
    low_confidence_threshold: float = DEFAULT_LOW_CONFIDENCE_THRESHOLD,
) -> OCRResult:
    """Translate a successful Azure Analyze response into a reviewable transcript."""

    analyze = payload.get("analyzeResult")
    if not isinstance(analyze, Mapping):
        # This parser may also be used with a bare AnalyzeResult in tests/tools.
        analyze = payload
    source_text = _provider_text(analyze.get("content"))
    pages = analyze.get("pages") or []
    if not isinstance(pages, list):
        pages = []

    page_lines: list[list[str]] = []
    confidence_scores: list[float] = []
    low_words: list[OCRWord] = []
    for index, raw_page in enumerate(pages, start=1):
        page = raw_page if isinstance(raw_page, Mapping) else {}
        page_number = int(_as_float(page.get("pageNumber")) or index)
        lines: list[str] = []
        raw_lines = page.get("lines") or []
        if isinstance(raw_lines, list):
            for raw_line in raw_lines:
                if isinstance(raw_line, Mapping):
                    line = _provider_text(raw_line.get("content"))
                    if line:
                        lines.append(line)
        page_lines.append(lines)

        raw_words = page.get("words") or []
        if not isinstance(raw_words, list):
            continue
        for raw_word in raw_words:
            if not isinstance(raw_word, Mapping):
                continue
            score = _as_float(raw_word.get("confidence"))
            if score is None or not 0.0 <= score <= 1.0:
                continue
            confidence_scores.append(score)
            low_words.append(OCRWord(_provider_text(raw_word.get("content")), score, page_number))

    styles = analyze.get("styles") or []
    handwriting_values: list[float] = []
    handwriting_information_available = False
    if isinstance(styles, list):
        for style in styles:
            if not isinstance(style, Mapping) or "isHandwritten" not in style:
                continue
            handwriting_information_available = True
            if style.get("isHandwritten") is True:
                score = _as_float(style.get("confidence"))
                if score is not None and 0.0 <= score <= 1.0:
                    handwriting_values.append(score)

    threshold = _bounded_float(
        low_confidence_threshold,
        DEFAULT_LOW_CONFIDENCE_THRESHOLD,
        minimum=0.0,
        maximum=1.0,
    )
    metadata: dict[str, Any] = {"model_id": str(analyze.get("modelId") or "prebuilt-read")}
    if handwriting_values:
        metadata["handwriting_style_confidence"] = {
            "count": len(handwriting_values),
            "maximum": round(max(handwriting_values), 4),
        }

    return OCRResult(
        # Azure's top-level content is preserved verbatim. Page lines only
        # provide a fallback if the successful response has no content string.
        transcript=source_text or _join_page_lines(page_lines, source_text),
        provider_id=AZURE_DOCUMENT_INTELLIGENCE,
        provider_label=PROVIDER_LABELS[AZURE_DOCUMENT_INTELLIGENCE],
        page_count=len(pages) or None,
        confidence=_confidence_summary(confidence_scores, unit="word", threshold=threshold),
        handwriting_detected=(bool(handwriting_values) if handwriting_information_available else None),
        low_confidence_words=_sorted_low_confidence_words(low_words, threshold),
        metadata=metadata,
    )


def _azure_failure_message(payload: Mapping[str, Any]) -> str:
    error = payload.get("error") if isinstance(payload.get("error"), Mapping) else payload
    if not isinstance(error, Mapping):
        return "Azure Document Intelligence marked the analysis as failed."
    code = str(error.get("code") or "").strip()
    message = " ".join(str(error.get("message") or "Analysis failed.").split())[:240]
    return f"{code}: {message}".strip(": ")


def process_azure_document_intelligence(
    file_bytes: bytes,
    mime_type: str,
    config: Mapping[str, Any],
    *,
    session: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> OCRResult:
    """Submit one document to Azure Read and safely poll its analysis result."""

    validate_ocr_input(file_bytes, mime_type)
    missing = missing_configuration(AZURE_DOCUMENT_INTELLIGENCE, config)
    if missing:
        raise OCRNotConfiguredError(
            "Azure Document Intelligence is not configured: missing " + ", ".join(missing) + ".",
            provider_id=AZURE_DOCUMENT_INTELLIGENCE,
        )
    endpoint = _validated_azure_endpoint(_value(config, "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"))
    api_key = str(_value(config, "AZURE_DOCUMENT_INTELLIGENCE_KEY") or "").strip()
    if not api_key:
        raise OCRNotConfiguredError(
            "Azure Document Intelligence needs AZURE_DOCUMENT_INTELLIGENCE_KEY.",
            provider_id=AZURE_DOCUMENT_INTELLIGENCE,
        )

    api_version = str(_value(config, "AZURE_DOCUMENT_INTELLIGENCE_API_VERSION", "2024-11-30") or "2024-11-30")
    locale = str(_value(config, "AZURE_DOCUMENT_INTELLIGENCE_LOCALE", "en") or "").strip()
    submit_url = f"{endpoint}/documentintelligence/documentModels/prebuilt-read:analyze"
    params: dict[str, str] = {
        "_overload": "analyzeDocument",
        "api-version": api_version,
    }
    if locale:
        params["locale"] = locale
    headers = {
        "Ocp-Apim-Subscription-Key": api_key,
        "Content-Type": "application/json",
    }
    request_timeout = _bounded_float(
        _value(config, "OCR_REQUEST_TIMEOUT_SECONDS", 90),
        90.0,
        minimum=5.0,
        maximum=300.0,
    )
    http = session or _requests_session()
    try:
        response = http.post(
            submit_url,
            params=params,
            headers=headers,
            json={"base64Source": base64.b64encode(bytes(file_bytes)).decode("ascii")},
            allow_redirects=False,
            timeout=request_timeout,
        )
    except Exception as exc:  # noqa: BLE001 - callers decide whether network errors are transient
        raise OCRProviderError(
            "Azure Document Intelligence request could not reach the service.",
            provider_id=AZURE_DOCUMENT_INTELLIGENCE,
        ) from exc

    _raise_for_unsuccessful_response(response, AZURE_DOCUMENT_INTELLIGENCE, "analysis request")
    operation_url = _header(_response_headers(response), "Operation-Location")
    if not operation_url or not _azure_operation_url_is_safe(operation_url, endpoint):
        raise OCRProviderError(
            "Azure Document Intelligence returned an invalid analysis operation location.",
            provider_id=AZURE_DOCUMENT_INTELLIGENCE,
        )

    poll_timeout = _bounded_float(
        _value(config, "AZURE_DOCUMENT_INTELLIGENCE_POLL_TIMEOUT_SECONDS", 120),
        120.0,
        minimum=5.0,
        maximum=600.0,
    )
    interval = _bounded_float(
        _value(config, "AZURE_DOCUMENT_INTELLIGENCE_POLL_INTERVAL_SECONDS", 1.0),
        1.0,
        minimum=0.2,
        maximum=20.0,
    )
    deadline = time.monotonic() + poll_timeout
    first_delay = _retry_after_seconds(response)
    if first_delay:
        sleep(min(first_delay, interval * 10))

    while time.monotonic() <= deadline:
        try:
            poll_response = http.get(
                operation_url,
                headers={"Ocp-Apim-Subscription-Key": api_key},
                allow_redirects=False,
                timeout=request_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise OCRProviderError(
                "Azure Document Intelligence result polling could not reach the service.",
                provider_id=AZURE_DOCUMENT_INTELLIGENCE,
            ) from exc
        _raise_for_unsuccessful_response(poll_response, AZURE_DOCUMENT_INTELLIGENCE, "analysis polling")
        payload = _response_json(poll_response, AZURE_DOCUMENT_INTELLIGENCE, "analysis polling")
        status = str(payload.get("status") or "").strip().lower()
        if status == "succeeded":
            result = parse_azure_document_intelligence_response(
                payload,
                low_confidence_threshold=_bounded_float(
                    _value(config, "OCR_LOW_CONFIDENCE_THRESHOLD", DEFAULT_LOW_CONFIDENCE_THRESHOLD),
                    DEFAULT_LOW_CONFIDENCE_THRESHOLD,
                    minimum=0.0,
                    maximum=1.0,
                ),
            )
            metadata = dict(result.metadata or {})
            metadata["api_version"] = api_version
            return OCRResult(
                transcript=result.transcript,
                provider_id=result.provider_id,
                provider_label=result.provider_label,
                page_count=result.page_count,
                confidence=result.confidence,
                handwriting_detected=result.handwriting_detected,
                low_confidence_words=result.low_confidence_words,
                metadata=metadata,
            )
        if status in {"failed", "canceled", "cancelled", "partiallySucceeded".lower()}:
            raise OCRProviderError(
                _azure_failure_message(payload),
                provider_id=AZURE_DOCUMENT_INTELLIGENCE,
            )
        if status not in {"notstarted", "running"}:
            raise OCRProviderError(
                "Azure Document Intelligence returned an unknown analysis status.",
                provider_id=AZURE_DOCUMENT_INTELLIGENCE,
            )
        delay = _retry_after_seconds(poll_response)
        sleep(min(delay if delay is not None else interval, interval * 10))

    raise OCRProviderError(
        "Azure Document Intelligence did not finish before the configured polling timeout.",
        provider_id=AZURE_DOCUMENT_INTELLIGENCE,
        status_code=504,
    )


# ---------------------------------------------------------------------------
# Public selector
# ---------------------------------------------------------------------------
def run_ocr(
    provider_id: str,
    file_bytes: bytes,
    mime_type: str,
    config: Mapping[str, Any],
    **kwargs: Any,
) -> OCRResult:
    """Run one selected benchmark provider through a provider-neutral interface."""

    canonical = canonical_provider_id(provider_id)
    if canonical == GOOGLE_DOCUMENT_AI:
        allowed = {"session", "access_token_getter"}
        return process_google_document_ai(
            file_bytes,
            mime_type,
            config,
            **{key: value for key, value in kwargs.items() if key in allowed},
        )
    allowed = {"session", "sleep"}
    return process_azure_document_intelligence(
        file_bytes,
        mime_type,
        config,
        **{key: value for key, value in kwargs.items() if key in allowed},
    )


def fingerprint_bytes(file_bytes: bytes) -> str:
    """Return a non-reversible identifier for associating ephemeral UI results."""

    return hashlib.sha256(bytes(file_bytes)).hexdigest()
