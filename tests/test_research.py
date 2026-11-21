"""Unit tests for Agent 1: Research Agent."""

import sys
import unittest
from pathlib import Path

# Add yt-agent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.research import ResearchAgent, ResearchInput
from models.research import ResearchBundle


class TestResearchAgent(unittest.TestCase):
    """Test suite for ResearchAgent."""

    def test_research_generates_valid_bundle(self):
        """Verify ResearchAgent produces valid Pydantic v2 ResearchBundle."""
        agent = ResearchAgent()
        bundle = agent.run(ResearchInput(topic="How Revolut Makes Money", pillar="finance_to_technical"))
        
        self.assertIsInstance(bundle, ResearchBundle)
        self.assertEqual(bundle.topic, "How Revolut Makes Money")
        self.assertEqual(bundle.pillar, "finance_to_technical")
        self.assertGreaterEqual(len(bundle.sources), 10)
        self.assertGreaterEqual(len(bundle.financials.revenue_by_year), 1)
        self.assertGreater(bundle.financials.margins.gross, 0.0)
        self.assertGreater(bundle.research_confidence, 0.8)

    def test_research_tech_to_financial_pillar(self):
        """Verify tech_to_financial pillar research generation."""
        agent = ResearchAgent()
        bundle = agent.run(ResearchInput(topic="Solana Pay Architecture", pillar="tech_to_financial"))
        
        self.assertEqual(bundle.pillar, "tech_to_financial")
        self.assertIn("consensus", bundle.technical_context.lower())
        self.assertGreaterEqual(len(bundle.competitors), 1)


if __name__ == "__main__":
    unittest.main()
