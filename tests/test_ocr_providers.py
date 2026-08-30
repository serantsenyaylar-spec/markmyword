"""Offline tests for the Azure-only OCR adapter.

All HTTP responses are canned. These tests never use a credential or send a
student document to Azure.
"""

from __future__ import annotations

import base64
from io import BytesIO
import unittest
from typing import Any

from pypdf import PdfWriter

from ocr_providers import (
    AZURE_DOCUMENT_INTELLIGENCE,
    AZURE_F0_MAX_INPUT_BYTES,
    OCRNotConfiguredError,
    OCRPolicyError,
    OCRProviderError,
    _azure_operation_url_is_safe,
    azure_input_limits,
    parse_azure_document_intelligence_response,
    process_azure_document_intelligence,
    validate_azure_input,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\nde-identified-page"


def pdf_bytes(page_count: int) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class FakeResponse:
    def __init__(self, status_code: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> Any:
        return self._payload


class AzureSession:
    def __init__(self, poll_responses: list[FakeResponse], operation_url: str) -> None:
        self.poll_responses = list(poll_responses)
        self.operation_url = operation_url
        self.post_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.post_calls.append({"url": url, **kwargs})
        return FakeResponse(202, {}, {"Operation-Location": self.operation_url, "Retry-After": "0"})

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.get_calls.append({"url": url, **kwargs})
        return self.poll_responses.pop(0)


def azure_success_response() -> dict[str, Any]:
    return {
        "status": "succeeded",
        "analyzeResult": {
            # The terminal newline proves no trimming/normalization occurs.
            "content": "First line\nSecond line\n",
            "modelId": "prebuilt-read",
            "pages": [
                {
                    "pageNumber": 1,
                    "words": [
                        {"content": "First", "confidence": 0.98},
                        {"content": "line", "confidence": 0.96},
                        {"content": "Second", "confidence": 0.45},
                    ],
                }
            ],
            "styles": [{"isHandwritten": True, "confidence": 0.91}],
        },
    }


def f0_config() -> dict[str, str]:
    return {
        "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT": "https://istek-ocr.cognitiveservices.azure.com",
        "AZURE_DOCUMENT_INTELLIGENCE_KEY": "not-a-real-key",
        "AZURE_DOCUMENT_INTELLIGENCE_PRICING_TIER": "F0",
        "AZURE_DOCUMENT_INTELLIGENCE_LOCALE": "en",
    }


class AzureOCRTests(unittest.TestCase):
    def test_parser_preserves_provider_text_and_review_metadata(self) -> None:
        result = parse_azure_document_intelligence_response(azure_success_response(), low_confidence_threshold=0.8)

        self.assertEqual(result.transcript, "First line\nSecond line\n")
        self.assertEqual(result.provider_id, AZURE_DOCUMENT_INTELLIGENCE)
        self.assertTrue(result.handwriting_detected)
        self.assertEqual(result.confidence["count"], 3)
        self.assertEqual(result.confidence["low_confidence_count"], 1)
        self.assertEqual(result.low_confidence_words[0].text, "Second")
        self.assertEqual(result.review_metadata()["source"], AZURE_DOCUMENT_INTELLIGENCE)

    def test_f0_rejects_a_three_page_pdf_before_http(self) -> None:
        session = AzureSession([], "https://istek-ocr.cognitiveservices.azure.com/unused")

        with self.assertRaises(OCRPolicyError) as raised:
            process_azure_document_intelligence(
                pdf_bytes(3),
                "application/pdf",
                f0_config(),
                session=session,
                sleep=lambda _: None,
            )

        self.assertIn("at most 2 PDF page", str(raised.exception))
        self.assertEqual(session.post_calls, [])

    def test_f0_rejects_more_than_four_megabytes_before_http(self) -> None:
        oversized_png = b"\x89PNG\r\n\x1a\n" + (b"x" * (AZURE_F0_MAX_INPUT_BYTES - 7))

        with self.assertRaises(OCRPolicyError):
            validate_azure_input(oversized_png, "image/png", f0_config())

    def test_s0_allows_a_three_page_pdf_inside_the_application_cap(self) -> None:
        config = {**f0_config(), "AZURE_DOCUMENT_INTELLIGENCE_PRICING_TIER": "S0"}

        limits = validate_azure_input(pdf_bytes(3), "application/pdf", config)

        self.assertEqual(limits.pricing_tier, "S0")
        self.assertEqual(limits.max_pdf_pages, 30)

    def test_unspecified_tier_fails_closed_to_f0(self) -> None:
        config = f0_config()
        del config["AZURE_DOCUMENT_INTELLIGENCE_PRICING_TIER"]

        limits = azure_input_limits(config)

        self.assertEqual(limits.pricing_tier, "F0")
        self.assertEqual(limits.max_pdf_pages, 2)

    def test_submission_polls_only_the_configured_host_and_disables_redirects(self) -> None:
        endpoint = f0_config()["AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"]
        operation = endpoint + "/documentintelligence/documentModels/prebuilt-read/analyzeResults/abc?api-version=2024-11-30"
        session = AzureSession([FakeResponse(200, azure_success_response())], operation)

        result = process_azure_document_intelligence(
            PNG_BYTES,
            "image/png",
            f0_config(),
            session=session,
            sleep=lambda _: None,
        )

        self.assertEqual(result.transcript, "First line\nSecond line\n")
        self.assertEqual(result.metadata["pricing_tier"], "F0")
        self.assertEqual(result.metadata["api_version"], "2024-11-30")
        self.assertEqual(len(session.post_calls), 1)
        self.assertEqual(len(session.get_calls), 1)
        post = session.post_calls[0]
        self.assertEqual(post["params"]["api-version"], "2024-11-30")
        self.assertEqual(post["params"]["locale"], "en")
        self.assertFalse(post["allow_redirects"])
        self.assertFalse(session.get_calls[0]["allow_redirects"])
        self.assertEqual(base64.b64decode(post["json"]["base64Source"]), PNG_BYTES)

    def test_invalid_bytes_are_rejected_before_http(self) -> None:
        session = AzureSession([], "https://istek-ocr.cognitiveservices.azure.com/unused")

        with self.assertRaises(OCRPolicyError):
            process_azure_document_intelligence(
                b"not really a PNG",
                "image/png",
                f0_config(),
                session=session,
                sleep=lambda _: None,
            )

        self.assertEqual(session.post_calls, [])

    def test_invalid_endpoint_is_rejected_before_http(self) -> None:
        session = AzureSession([], "https://istek-ocr.cognitiveservices.azure.com/unused")
        config = {**f0_config(), "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT": "https://resource.example:bad"}

        with self.assertRaises(OCRNotConfiguredError):
            process_azure_document_intelligence(
                PNG_BYTES,
                "image/png",
                config,
                session=session,
                sleep=lambda _: None,
            )

        self.assertEqual(session.post_calls, [])

    def test_operation_url_requires_the_configured_https_host(self) -> None:
        endpoint = f0_config()["AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"]
        self.assertTrue(
            _azure_operation_url_is_safe(
                "https://istek-ocr.cognitiveservices.azure.com:443/documentintelligence/analyzeResults/abc",
                endpoint,
            )
        )
        self.assertFalse(
            _azure_operation_url_is_safe("http://istek-ocr.cognitiveservices.azure.com/result", endpoint)
        )
        self.assertFalse(_azure_operation_url_is_safe("https://user@istek-ocr.cognitiveservices.azure.com/result", endpoint))
        self.assertFalse(_azure_operation_url_is_safe("https://unexpected.example.test/result", endpoint))

    def test_other_host_operation_location_is_rejected(self) -> None:
        session = AzureSession([], "https://unexpected.example.test/analyzeResults/abc")

        with self.assertRaises(OCRProviderError):
            process_azure_document_intelligence(
                PNG_BYTES,
                "image/png",
                f0_config(),
                session=session,
                sleep=lambda _: None,
            )

        self.assertEqual(session.get_calls, [])


if __name__ == "__main__":
    unittest.main()
