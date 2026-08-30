"""Offline regression tests for the Azure OCR configuration template."""

from __future__ import annotations

from pathlib import Path
import tomllib
import unittest

from ocr_providers import azure_input_limits


class AzureOCRConfigurationTests(unittest.TestCase):
    def test_template_has_azure_f0_guard(self) -> None:
        template = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml.example"
        with template.open("rb") as handle:
            config = tomllib.load(handle)

        self.assertEqual(config["AZURE_DOCUMENT_INTELLIGENCE_PRICING_TIER"], "F0")
        self.assertIn("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", config)
        limits = azure_input_limits(config)
        self.assertEqual(limits.max_input_bytes, 4 * 1024 * 1024)
        self.assertEqual(limits.max_pdf_pages, 2)
        self.assertEqual(
            config["auth"]["google"]["client_id"],
            "xxxxxxxxxxxx-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.apps.googleusercontent.com",
        )


if __name__ == "__main__":
    unittest.main()
