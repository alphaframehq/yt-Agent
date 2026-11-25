"""Unit tests for Phase 2: External Tools.

2026-11-25 rewrite: the old tests asserted that every tool returned
fabricated data without credentials (fake images, invented financials,
fabricated articles, fake YouTube research, silence audio). Those
mock/demo fallbacks are removed. These tests now assert the new
fail-loud behaviour: missing keys / failed APIs raise instead of
returning fake success.
"""

import os
import sys
import unittest
from pathlib import Path

# Ensure yt-agent root is on python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests._sheets_fake import install_fake_sheets

# Google Sheets is the data store now (DynamoDB removed 25 Nov 2026).
# Tests must never touch the real Sheets API — mock the client.
# (These tests previously relied on ALLOW_IN_MEMORY_CONFIG; the fake
# replaces that dev-only fallback. Installed in setUpModule; stopped in tearDownModule.)
_FAKE_SHEETS = None
_SHEETS_PATCHER = None


def setUpModule():
    global _FAKE_SHEETS, _SHEETS_PATCHER
    _FAKE_SHEETS, _SHEETS_PATCHER = install_fake_sheets()


def tearDownModule():
    if _SHEETS_PATCHER is not None:
        _SHEETS_PATCHER.stop()

from tools.llm_provider import LLMProvider, call_with_fallback
from tools.image_gen import generate_image, get_cached_image_path
from tools.sec_edgar import search_company_filings
from tools.yfinance_tool import get_ticker_financials
from tools.tinyfish import search_fintech_context
from tools.youtube_api import research_topic
from tools.pixabay import search_background_music
from tools.polly import format_ssml, synthesize_speech


class TestPhase2Tools(unittest.TestCase):
    """Test suite covering all external tool integrations."""

    def test_llm_provider_initialization(self):
        """Verify LLM provider instances initialize correctly."""
        p1 = LLMProvider("gemini", "gemini-3.8-flash")
        self.assertEqual(p1.provider, "gemini")
        self.assertEqual(p1.model, "gemini-3.8-flash")

        p2 = LLMProvider("bedrock", "claude-3-5-haiku")
        self.assertEqual(p2.provider, "bedrock")

    def test_llm_provider_raises_without_key(self):
        """Without a Gemini key, the provider must raise instead of
        returning a fake '{}' success (which used to short-circuit the
        OpenRouter fallback)."""
        provider = LLMProvider("gemini", "gemini-3.8-flash")
        with self.assertRaises(Exception):
            provider.call("You are a test assistant.", "test prompt")

    def test_ssml_formatting(self):
        """Verify SSML builder sanitizes XML and applies prosody rules."""
        raw = "Stripe's revenue grew by 25% in 2024 & processed $1T."
        ssml = format_ssml(raw)
        self.assertIn('<speak><prosody rate="+5%">', ssml)
        self.assertIn("&amp;", ssml)
        self.assertIn('<break time="800ms"/></speak>', ssml)

    def test_image_cache_path_deterministic(self):
        """Verify image caching key is deterministic MD5 hash."""
        prompt = "Modern high-frequency trading server rack"
        model = "gemini/imagen-3.0-generate-001"
        p1 = get_cached_image_path(prompt, model)
        p2 = get_cached_image_path(prompt, model)
        self.assertEqual(p1, p2)
        self.assertEqual(p1.suffix, ".png")

    def test_image_gen_raises_without_key(self):
        """Without an image-generation key, generate_image must raise
        instead of returning a Pillow placeholder card / 0-byte file."""
        with self.assertRaises(Exception):
            generate_image("Test Fintech Hook", model="gemini/imagen-3.0-generate-001")

    def test_yfinance_tool_structure(self):
        """Verify yfinance returns expected dictionary schema with margins."""
        data = get_ticker_financials("AAPL")
        self.assertIn("symbol", data)
        self.assertIn("margins", data)
        self.assertIn("gross", data["margins"])
        self.assertIn("revenue_by_year", data)

    def test_sec_edgar_lookup(self):
        """Verify SEC EDGAR ticker resolver."""
        res = search_company_filings("AAPL")
        self.assertEqual(res["ticker"], "AAPL")

    def test_tinyfish_raises_without_key(self):
        """Without a TinyFish key, search must raise instead of returning
        fabricated articles with fake URLs."""
        with self.assertRaises(Exception):
            search_fintech_context("Stripe interchange fees")

    def test_youtube_api_topic_research_raises_without_service(self):
        """Without YouTube credentials, research_topic must raise instead
        of returning invented titles/tags."""
        with self.assertRaises(Exception):
            research_topic("How Revolut Makes Money")

    def test_polly_tts_synthesis_raises_without_aws(self):
        """Without AWS Polly credentials, synthesis must raise instead of
        returning a 1-second silence stub."""
        with self.assertRaises(Exception):
            synthesize_speech("Welcome to TubePilot AI financial breakdowns.")


if __name__ == "__main__":
    unittest.main()
