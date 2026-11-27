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
import time
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

    def test_tinyfish_missing_key_falls_back_to_real_rss_not_fabricated(self):
        """Without a TinyFish key, search must fall back to REAL RSS
        articles (26 Nov 2026, run #14) — never fabricated articles
        with fake URLs. The old contract (raise without a key) died
        with the old TinySearch host: a missing/dead TinySearch must
        no longer hard-fail the whole daily run."""
        from unittest.mock import patch

        from tools.trends import TrendItem, TrendReport

        with (
            patch("tools.tinyfish.get_secret", return_value=None),
            patch(
                "tools.trends.fetch_trends",
                return_value=TrendReport(
                    topic="Stripe interchange fees",
                    pillar="ai_everything",
                    angles=[],
                    items=[
                        TrendItem(
                            title="Stripe Cuts Interchange Fees",
                            source="google_news",
                            url="https://example.com/news/stripe-fees",
                            snippet="Stripe lowers fees for small businesses.",
                            # Fresh (relative to now) — the 7-day max-age
                            # filter (27 Nov 2026) drops stale items.
                            published_at=int(time.time()) - 3600,
                        )
                    ],
                    sources=["google_news"],
                    errors=[],
                ),
            ),
        ):
            articles = search_fintech_context("Stripe interchange fees")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["url"], "https://example.com/news/stripe-fees")
        self.assertNotIn("alphapilot.io", articles[0]["url"])
        self.assertNotIn("fintechinsights.io", articles[0]["url"])

    def test_tinyfish_raises_when_both_sources_dead(self):
        """Missing key AND empty RSS -> loud failure, never invented."""
        from unittest.mock import patch

        from tools.trends import TrendReport

        with (
            patch("tools.tinyfish.get_secret", return_value=None),
            patch(
                "tools.trends.fetch_trends",
                return_value=TrendReport(
                    topic="Stripe interchange fees",
                    pillar="ai_everything",
                    angles=[],
                    items=[],
                    sources=[],
                    errors=["google_news: boom"],
                ),
            ),
        ):
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
