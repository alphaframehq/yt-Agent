"""Unit tests for Phase 2: External Tools."""

import os
import sys
import unittest
from pathlib import Path

# Ensure yt-agent root is on python path
sys.path.insert(0, str(Path(__file__).parent.parent))

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
        p1 = LLMProvider("gemini", "gemini-2.0-flash")
        self.assertEqual(p1.provider, "gemini")
        self.assertEqual(p1.model, "gemini-2.0-flash")

        p2 = LLMProvider("bedrock", "claude-3-5-haiku")
        self.assertEqual(p2.provider, "bedrock")

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

    def test_image_gen_fallback(self):
        """Verify fallback image generation creates valid visual asset."""
        img_path = generate_image("Test Fintech Hook", model="gemini/imagen-3.0-generate-001")
        self.assertTrue(os.path.exists(img_path))
        self.assertGreater(Path(img_path).stat().st_size, 0)

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

    def test_tinyfish_fintech_context(self):
        """Verify fintech news/trend context gathering."""
        articles = search_fintech_context("Stripe interchange fees")
        self.assertGreater(len(articles), 0)
        self.assertIn("title", articles[0])
        self.assertIn("snippet", articles[0])

    def test_youtube_api_topic_research(self):
        """Verify YouTube competitor insights extraction."""
        insights = research_topic("How Revolut Makes Money")
        self.assertIn("top_titles", insights)
        self.assertGreater(len(insights["top_titles"]), 0)
        self.assertIn("common_tags", insights)
        self.assertGreaterEqual(len(insights["common_tags"]), 5)

    def test_polly_tts_synthesis(self):
        """Verify speech synthesis returns audio file and positive duration."""
        audio_path, duration = synthesize_speech("Welcome to TubePilot AI financial breakdowns.")
        self.assertTrue(os.path.exists(audio_path))
        self.assertGreater(duration, 0.0)


if __name__ == "__main__":
    unittest.main()
