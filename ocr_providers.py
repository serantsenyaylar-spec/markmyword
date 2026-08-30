"""Server-side Azure Document Intelligence Read OCR adapter for Mark My Words.

This module intentionally contains only Azure OCR. No secondary provider or
fallback is configured in this Azure-only application path.

The adapter returns the provider's transcript without spell correction or other
silent rewriting. It validates file type, size, and page limits before any
Azure request, keeps credentials server-side, and exposes concise provenance
and review metadata for the teacher-facing workflow.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any
from urllib.parse import urlsplit

AZURE_DOCUMENT_INTELLIGENCE = "azure_document_intelligence"
AZURE_PROVIDER_LABEL = "Azure Document Intelligence — Read"

SUPPORTED_MIME_TYPES = frozenset({"application/pdf", "image/jpeg", "image/png"})

# Mark My Words' general upload ceiling. Azure S0 processing is kept inside this
# conservative application bound; Azure F0 has stricter limits below.
MAX_OCR_INPUT_BYTES = 10 * 1024 * 1024
MAX_OCR_PDF_PAGES = 30

# Azure's F0/free resource processes only the first two PDF pages and accepts a
# 4 MB document. Enforce those constraints locally so the app never presents a
# partial F0 result as a full student submission.
AZURE_F0_MAX_INPUT_BYTES = 4 * 1024 * 1024
AZURE_F0_MAX_PDF_PAGES = 2
AZURE_PRICING_TIERS = frozenset({"F0", "S0"})

DEFAULT_LOW_CONFIDENCE_THRESHOLD = 0.80


class OCRProviderError(RuntimeError):
    """A concise, safe Azure OCR failure suitable for the application UI."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class OCRNotConfiguredError(OCRProviderError):
    """Raised when required Azure server-side configuration is absent or invalid."""


class OCRPolicyError(OCRProviderError):
    """Raised before HTTP when an input violates an application safety limit."""


@dataclass(frozen=True)
class OCRWord:
    """A provider OCR item a teacher may want to verify against the scan."""

    text: str
    confidence: float
    page_number: int | None = None


@dataclass(frozen=True)
class OCRResult:
    """Azure OCR output, with transcript separate from non-content metadata."""

    transcript: str
    page_count: int | None
    confidence: Mapping[str, Any] | None = None
    handwriting_detected: bool | None = None
    low_confidence_words: tuple[OCRWord, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def provider_id(self) -> str:
        return AZURE_DOCUMENT_INTELLIGENCE

    @property
    def provider_label(self) -> str:
        return AZURE_PROVIDER_LABEL

    def review_metadata(self) -> dict[str, Any]:
        """Return provenance safe for app state without a raw Azure response."""

        output = dict(self.metadata or {})
        output.update(
            {
                "source": AZURE_DOCUMENT_INTELLIGENCE,
                "provider_id": AZURE_DOCUMENT_INTELLIGENCE,
                "provider": AZURE_PROVIDER_LABEL,
                "page_count": self.page_count,
                "handwriting_detected": self.handwriting_detected,
            }
        )
        if self.confidence:
            output["confidence"] = dict(self.confidence)
        return output


@dataclass(frozen=True)
class AzureOCRLimits:
    """Effective application limits for the Azure pricing tier in use."""

    pricing_tier: str
    max_input_bytes: int
    max_pdf_pages: int


# ---------------------------------------------------------------------------
# Configuration and local input policy
# ---------------------------------------------------------------------------
def _value(config: Mapping[str, Any], key: str, default: Any = "") -> Any:
    value = config.get(key, default)
    return default if value is None else value


def _nonempty(value: Any) -> bool:
    return bool(value.strip()) if isinstance(value, str) else value is not None


def missing_azure_configuration(config: Mapping[str, Any]) -> list[str]:
    """List only missing setting names, never their values."""

    required = (
        "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
        "AZURE_DOCUMENT_INTELLIGENCE_KEY",
    )
    return [key for key in required if not _nonempty(_value(config, key))]


def azure_pricing_tier(config: Mapping[str, Any]) -> str:
    """Return F0 or S0, defaulting conservatively to F0 when unspecified."""

    raw = str(_value(config, "AZURE_DOCUMENT_INTELLIGENCE_PRICING_TIER", "F0") or "F0").strip().upper()
    aliases = {"FREE": "F0", "FREE_TIER": "F0", "STANDARD": "S0"}
    tier = aliases.get(raw, raw)
    if tier not in AZURE_PRICING_TIERS:
        raise OCRNotConfiguredError(
            "AZURE_DOCUMENT_INTELLIGENCE_PRICING_TIER must be F0 or S0."
        )
    return tier


def azure_input_limits(config: Mapping[str, Any]) -> AzureOCRLimits:
    """Return fail-closed limits for the configured Azure pricing tier."""

    tier = azure_pricing_tier(config)
    if tier == "F0":
        return AzureOCRLimits(
            pricing_tier=tier,
            max_input_bytes=AZURE_F0_MAX_INPUT_BYTES,
            max_pdf_pages=AZURE_F0_MAX_PDF_PAGES,
        )
    return AzureOCRLimits(
        pricing_tier=tier,
        max_input_bytes=MAX_OCR_INPUT_BYTES,
        max_pdf_pages=MAX_OCR_PDF_PAGES,
    )


def _pdf_page_count(file_bytes: bytes) -> int:
    """Inspect a PDF locally; encrypted/unreadable documents are not uploaded."""

    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(file_bytes))
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise OCRPolicyError("Password-protected PDFs cannot be sent to Azure OCR.")
        return len(reader.pages)
    except OCRPolicyError:
        raise
    except Exception as exc:  # noqa: BLE001 - unsafe PDFs must fail closed before HTTP
        raise OCRPolicyError("The PDF could not be safely inspected before Azure OCR.") from exc


def validate_azure_input(
    file_bytes: bytes,
    mime_type: str,
    config: Mapping[str, Any],
) -> AzureOCRLimits:
    """Validate bytes and effective tier limits before authentication or HTTP."""

    if not isinstance(file_bytes, (bytes, bytearray)) or not file_bytes:
        raise OCRPolicyError("Azure OCR needs a non-empty PDF, JPG, or PNG file.")
    if mime_type not in SUPPORTED_MIME_TYPES:
        supported = ", ".join(sorted(SUPPORTED_MIME_TYPES))
        raise OCRPolicyError(f"Azure OCR supports only: {supported}.")

    content = bytes(file_bytes)
    if len(content) > MAX_OCR_INPUT_BYTES:
        raise OCRPolicyError(
            f"OCR input is over the {MAX_OCR_INPUT_BYTES // (1024 * 1024)} MB application safety limit."
        )

    signatures = {
        "application/pdf": b"%PDF-" in content[:1024],
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
    }
    if not signatures[mime_type]:
        raise OCRPolicyError("The OCR file bytes do not match the claimed PDF, JPG, or PNG type.")

    limits = azure_input_limits(config)
    if len(content) > limits.max_input_bytes:
        raise OCRPolicyError(
            f"Azure {limits.pricing_tier} accepts at most "
            f"{limits.max_input_bytes // (1024 * 1024)} MB per OCR request."
        )
    if mime_type == "application/pdf":
        page_count = _pdf_page_count(content)
        if page_count > limits.max_pdf_pages:
            raise OCRPolicyError(
                f"Azure {limits.pricing_tier} accepts at most {limits.max_pdf_pages} PDF page(s) per OCR request; "
                f"this PDF has {page_count}."
            )
    return limits


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------
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
    return default if parsed is None else max(minimum, min(maximum, parsed))


def _response_status_code(response: Any) -> int | None:
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


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
    """Return a short diagnostic without retaining a raw response payload."""

    code = ""
    message = ""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - Azure response decoding is untrusted
        body = None
    if isinstance(body, Mapping):
        error = body.get("error", body)
        if isinstance(error, Mapping):
            code = str(error.get("code") or "").strip()
            message = str(error.get("message") or "").strip()
    message = " ".join(message.split())[:240] if message else "Azure rejected the OCR request."
    return f"{code}: {message}".strip(": ")


def _raise_for_unsuccessful_response(response: Any, action: str) -> None:
    status = _response_status_code(response)
    if status is not None and 200 <= status < 300:
        return
    status_display = str(status) if status is not None else "unknown HTTP status"
    raise OCRProviderError(
        f"Azure Document Intelligence {action} failed ({status_display}): "
        f"{_safe_provider_error_text(response)}",
        status_code=status,
        retry_after_seconds=_retry_after_seconds(response),
    )


def _response_json(response: Any, action: str) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - do not expose a raw response body
        raise OCRProviderError(
            f"Azure Document Intelligence {action} returned invalid JSON.",
            status_code=_response_status_code(response),
        ) from exc
    if not isinstance(payload, Mapping):
        raise OCRProviderError(
            f"Azure Document Intelligence {action} returned an unexpected response.",
            status_code=_response_status_code(response),
        )
    return payload


def _requests_session() -> Any:
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requirements install requests
        raise OCRProviderError("The requests package is not installed; install requirements.txt.") from exc
    return requests.Session()


# ---------------------------------------------------------------------------
# Azure response parser
# ---------------------------------------------------------------------------
def _confidence_summary(values: list[float], threshold: float) -> dict[str, Any] | None:
    valid = [score for score in values if 0.0 <= score <= 1.0]
    if not valid:
        return None
    low_count = sum(score < threshold for score in valid)
    return {
        "unit": "word",
        "count": len(valid),
        "mean": round(sum(valid) / len(valid), 4),
        "minimum": round(min(valid), 4),
        "maximum": round(max(valid), 4),
        "low_confidence_threshold": round(threshold, 4),
        "low_confidence_count": low_count,
        "low_confidence_rate": round(low_count / len(valid), 4),
    }


def _provider_text(value: Any) -> str:
    """Return an Azure text value exactly as supplied, never spell-corrected."""

    return value if isinstance(value, str) else ""


def _fallback_lines(pages: list[Any]) -> str:
    """Use raw Azure line strings only if a successful response omitted content."""

    page_text: list[str] = []
    for raw_page in pages:
        page = raw_page if isinstance(raw_page, Mapping) else {}
        lines = page.get("lines") or []
        if not isinstance(lines, list):
            continue
        page_text.append("\n".join(_provider_text(line.get("content")) for line in lines if isinstance(line, Mapping)))
    return "\n\n".join(page for page in page_text if page)


def _low_confidence_words(words: list[OCRWord], threshold: float) -> tuple[OCRWord, ...]:
    selected = [word for word in words if word.text and word.confidence < threshold]
    selected.sort(key=lambda word: (word.confidence, word.page_number or 0, word.text.casefold()))
    return tuple(selected[:50])


def parse_azure_document_intelligence_response(
    payload: Mapping[str, Any],
    *,
    low_confidence_threshold: float = DEFAULT_LOW_CONFIDENCE_THRESHOLD,
) -> OCRResult:
    """Translate Azure Read output without changing the provider transcript."""

    analyze = payload.get("analyzeResult")
    if not isinstance(analyze, Mapping):
        analyze = payload  # Allows a bare AnalyzeResult in offline tests/tools.

    source_text = _provider_text(analyze.get("content"))
    pages = analyze.get("pages") or []
    if not isinstance(pages, list):
        pages = []

    scores: list[float] = []
    words: list[OCRWord] = []
    for index, raw_page in enumerate(pages, start=1):
        page = raw_page if isinstance(raw_page, Mapping) else {}
        page_number = int(_as_float(page.get("pageNumber")) or index)
        raw_words = page.get("words") or []
        if not isinstance(raw_words, list):
            continue
        for raw_word in raw_words:
            if not isinstance(raw_word, Mapping):
                continue
            score = _as_float(raw_word.get("confidence"))
            if score is None or not 0.0 <= score <= 1.0:
                continue
            scores.append(score)
            words.append(OCRWord(_provider_text(raw_word.get("content")), score, page_number))

    styles = analyze.get("styles") or []
    handwriting_information_available = False
    handwritten_flags: list[bool] = []
    handwritten_confidences: list[float] = []
    if isinstance(styles, list):
        for style in styles:
            if not isinstance(style, Mapping) or "isHandwritten" not in style:
                continue
            handwriting_information_available = True
            is_handwritten = style.get("isHandwritten") is True
            handwritten_flags.append(is_handwritten)
            if is_handwritten:
                score = _as_float(style.get("confidence"))
                if score is not None and 0.0 <= score <= 1.0:
                    handwritten_confidences.append(score)

    threshold = _bounded_float(
        low_confidence_threshold,
        DEFAULT_LOW_CONFIDENCE_THRESHOLD,
        minimum=0.0,
        maximum=1.0,
    )
    metadata: dict[str, Any] = {"model_id": str(analyze.get("modelId") or "prebuilt-read")}
    if handwritten_confidences:
        metadata["handwriting_style_confidence"] = {
            "count": len(handwritten_confidences),
            "maximum": round(max(handwritten_confidences), 4),
        }

    return OCRResult(
        transcript=source_text or _fallback_lines(pages),
        page_count=len(pages) or None,
        confidence=_confidence_summary(scores, threshold),
        handwriting_detected=(any(handwritten_flags) if handwriting_information_available else None),
        low_confidence_words=_low_confidence_words(words, threshold),
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Azure REST call and safe polling
# ---------------------------------------------------------------------------
def _validated_azure_endpoint(value: Any) -> str:
    endpoint = str(value or "").strip().rstrip("/")
    parsed = urlsplit(endpoint)
    try:
        parsed.port  # Validate malformed explicit ports before HTTP.
    except ValueError:
        raise OCRNotConfiguredError(
            "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT must use a valid HTTPS port."
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
            "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT must be an HTTPS resource endpoint without a path."
        )
    return endpoint


def _azure_operation_url_is_safe(operation_url: str, endpoint: str) -> bool:
    """Only poll an HTTPS operation on the configured Azure host and port."""

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
    """Run Azure Read OCR after local validation; no filename or URL is sent."""

    limits = validate_azure_input(file_bytes, mime_type, config)
    missing = missing_azure_configuration(config)
    if missing:
        raise OCRNotConfiguredError("Azure OCR is not configured: missing " + ", ".join(missing) + ".")

    endpoint = _validated_azure_endpoint(_value(config, "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"))
    api_key = str(_value(config, "AZURE_DOCUMENT_INTELLIGENCE_KEY") or "").strip()
    if not api_key:
        raise OCRNotConfiguredError("Azure OCR needs AZURE_DOCUMENT_INTELLIGENCE_KEY.")

    api_version = str(_value(config, "AZURE_DOCUMENT_INTELLIGENCE_API_VERSION", "2024-11-30") or "2024-11-30")
    locale = str(_value(config, "AZURE_DOCUMENT_INTELLIGENCE_LOCALE", "en") or "").strip()
    request_timeout = _bounded_float(
        _value(config, "OCR_REQUEST_TIMEOUT_SECONDS", 90),
        90.0,
        minimum=5.0,
        maximum=300.0,
    )
    submit_url = f"{endpoint}/documentintelligence/documentModels/prebuilt-read:analyze"
    params: dict[str, str] = {"_overload": "analyzeDocument", "api-version": api_version}
    if locale:
        params["locale"] = locale
    http = session or _requests_session()

    try:
        response = http.post(
            submit_url,
            params=params,
            headers={
                "Ocp-Apim-Subscription-Key": api_key,
                "Content-Type": "application/json",
            },
            json={"base64Source": base64.b64encode(bytes(file_bytes)).decode("ascii")},
            allow_redirects=False,
            timeout=request_timeout,
        )
    except Exception as exc:  # noqa: BLE001 - transport details can expose configuration
        raise OCRProviderError("Azure OCR request could not reach the service.") from exc

    _raise_for_unsuccessful_response(response, "analysis request")
    operation_url = _header(_response_headers(response), "Operation-Location")
    if not operation_url or not _azure_operation_url_is_safe(operation_url, endpoint):
        raise OCRProviderError("Azure OCR returned an invalid analysis operation location.")

    poll_timeout = _bounded_float(
        _value(config, "AZURE_DOCUMENT_INTELLIGENCE_POLL_TIMEOUT_SECONDS", 120),
        120.0,
        minimum=5.0,
        maximum=600.0,
    )
    interval = _bounded_float(
        _value(config, "AZURE_DOCUMENT_INTELLIGENCE_POLL_INTERVAL_SECONDS", 1.0),
        1.0,
        minimum=1.0 if limits.pricing_tier == "F0" else 0.2,
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
        except Exception as exc:  # noqa: BLE001 - transport details can expose configuration
            raise OCRProviderError("Azure OCR result polling could not reach the service.") from exc

        _raise_for_unsuccessful_response(poll_response, "analysis polling")
        payload = _response_json(poll_response, "analysis polling")
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
            metadata.update(
                {
                    "api_version": api_version,
                    "pricing_tier": limits.pricing_tier,
                }
            )
            return OCRResult(
                transcript=result.transcript,
                page_count=result.page_count,
                confidence=result.confidence,
                handwriting_detected=result.handwriting_detected,
                low_confidence_words=result.low_confidence_words,
                metadata=metadata,
            )
        if status in {"failed", "canceled", "cancelled", "partiallysucceeded"}:
            raise OCRProviderError(_azure_failure_message(payload))
        if status not in {"notstarted", "running"}:
            raise OCRProviderError("Azure OCR returned an unknown analysis status.")

        retry_after = _retry_after_seconds(poll_response)
        sleep(min(retry_after if retry_after is not None else interval, interval * 10))

    raise OCRProviderError(
        "Azure OCR did not finish before the configured polling timeout.",
        status_code=504,
    )
