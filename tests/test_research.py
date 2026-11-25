"""Unit tests for Agent 1: Research Agent.

2026-11-25 rewrite: the old tests called run_research() for private
companies (Revolut) and unknown topics (Solana Pay) and asserted on the
fabricated bundle the old mock fallbacks produced. The P0 patches removed
those fallbacks: research now fails loudly instead of inventing
financials, sources, and tickers. These tests assert that fail-loud
behaviour. Real research is exercised only with real data sources, which
CI does not have.
"""

import os
import sys
import unittest
from pathlib import Path

# Add yt-agent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("ALLOW_IN_MEMORY_CONFIG", "1")
os.environ.setdefault("ALLOW_IN_MEMORY_SECRETS", "1")

from agents.research import ResearchAgent, ResearchInput


class TestResearchAgent(unittest.TestCase):
    """Test suite for ResearchAgent."""

    def test_private_company_research_fails_loudly(self):
        """Revolut is private: no public ticker/financials exist, so
        research must raise instead of mapping it to a public ticker
        (the old code mapped Revolut -> PYPL) and inventing numbers."""
        agent = ResearchAgent()
        with self.assertRaises(Exception):
            agent.run(ResearchInput(topic="How Revolut Makes Money", pillar="finance_to_technical"))

    def test_unknown_topic_research_fails_loudly(self):
        """An unknown topic must raise instead of silently defaulting
        to ticker SQ (Block/Square) and fabricating a bundle."""
        agent = ResearchAgent()
        with self.assertRaises(Exception):
            agent.run(ResearchInput(topic="Solana Pay Architecture", pillar="tech_to_financial"))

    def test_ticker_inference_rejects_private_companies(self):
        """_infer_ticker must return None (not a public ticker) for
        private companies and unknown topics — no SQ default, no
        Stripe->SQ / Revolut->PYPL mapping."""
        agent = ResearchAgent()
        for topic in ("How Stripe Makes Money", "How Revolut Makes Money", "Solana Pay Architecture"):
            self.assertIsNone(agent._infer_ticker(topic))


if __name__ == "__main__":
    unittest.main()
