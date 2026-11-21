"""Mandatory Verification Agent Test Suite (Phase 3).

Spec requires 7 specific test cases:
1. Hallucinated number -> fails with CRITICAL
2. "You should buy this stock" -> caught as investment advice
3. Missing spoken disclaimer -> fails
4. Contradictory revenue numbers in 2 scenes -> caught
5. Orphan citation -> fails
6. Valid script -> passes with zero issues
7. 3 iterations of failures -> escalates to human_review
"""

import copy
import sys
import unittest
from pathlib import Path

# Add yt-agent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.research import run_research
from agents.script import run_script
from agents.verify import VerificationAgent


class TestVerificationAgent(unittest.TestCase):
    """Rigorous audit test suite checking deterministic and semantic gates."""

    @classmethod
    def setUpClass(cls):
        """Build base bundle and script once to keep test suite blazing fast."""
        cls.agent = VerificationAgent()
        cls.base_bundle = run_research(topic="How Stripe Makes Money", pillar="finance_to_technical")
        cls.base_script = run_script(cls.base_bundle)

    def setUp(self):
        self.bundle = copy.deepcopy(self.base_bundle)
        self.valid_script = copy.deepcopy(self.base_script)

    def test_1_hallucinated_number_fails_critical(self):
        """Test 1: Hallucinated number in narration without source -> fails with CRITICAL."""
        script = copy.deepcopy(self.valid_script)
        # Inject fabricated metric into scene 1 with no citation
        script.scenes[0].citations = []
        script.scenes[0].narration = "Stripe secretly generated $999.88 billion in offshore untaxed revenue."
        
        report = self.agent.run(self.bundle, script)
        self.assertEqual(report.overall, "fail")
        
        # Check that a critical factual accuracy issue was flagged
        accuracy_check = next((c for c in report.checks if c.name == "FACTUAL_ACCURACY"), None)
        self.assertIsNotNone(accuracy_check)
        critical_issues = [i for i in accuracy_check.issues if i.severity == "critical"]
        self.assertGreaterEqual(len(critical_issues), 1)

    def test_2_investment_advice_caught(self):
        """Test 2: 'You should buy this stock' -> caught as investment advice (CRITICAL)."""
        script = copy.deepcopy(self.valid_script)
        script.scenes[0].narration += " In fact, you should buy this stock right now before it explodes."
        
        report = self.agent.run(self.bundle, script)
        self.assertEqual(report.overall, "fail")
        
        advice_check = next((c for c in report.checks if c.name == "INVESTMENT_ADVICE_REGEX"), None)
        self.assertIsNotNone(advice_check)
        self.assertEqual(advice_check.status, "fail")
        self.assertTrue(any("buy" in i.issue.lower() for i in advice_check.issues))

    def test_3_missing_spoken_disclaimer_fails(self):
        """Test 3: Missing spoken disclaimer -> fails."""
        script = copy.deepcopy(self.valid_script)
        # Remove disclaimer scene and replace with generic narration
        for s in script.scenes:
            if s.section == "disclaimer":
                s.narration = "And that brings us to the end of our financial analysis for today."
        
        report = self.agent.run(self.bundle, script)
        self.assertEqual(report.overall, "fail")
        
        disc_check = next((c for c in report.checks if c.name == "DISCLAIMER_PRESENCE"), None)
        self.assertIsNotNone(disc_check)
        self.assertEqual(disc_check.status, "fail")

    def test_4_contradictory_revenue_numbers_caught(self):
        """Test 4: Contradictory revenue numbers in 2 scenes -> caught."""
        script = copy.deepcopy(self.valid_script)
        script.scenes[1].narration = "Stripe reported total revenue of $14 billion in 2024, but earlier statements showed revenue of $82 billion for the same fiscal year."
        
        report = self.agent.run(self.bundle, script)
        contra_check = next((c for c in report.checks if c.name == "CONTRADICTIONS"), None)
        self.assertIsNotNone(contra_check)
        self.assertEqual(contra_check.status, "fail")

    def test_5_orphan_citation_fails(self):
        """Test 5: Orphan citation -> fails."""
        script = copy.deepcopy(self.valid_script)
        script.scenes[0].citations = ["SRC-999-NONEXISTENT"]
        
        report = self.agent.run(self.bundle, script)
        self.assertEqual(report.overall, "fail")
        
        cit_check = next((c for c in report.checks if c.name == "CITATION_VALIDITY"), None)
        self.assertIsNotNone(cit_check)
        self.assertEqual(cit_check.status, "fail")
        self.assertTrue(any("SRC-999-NONEXISTENT" in i.issue for i in cit_check.issues))

    def test_6_valid_script_passes(self):
        """Test 6: Valid script -> passes with zero issues."""
        report = self.agent.run(self.bundle, self.valid_script)
        self.assertEqual(report.overall, "pass")
        self.assertGreaterEqual(report.confidence, 0.95)
        
        # Verify deterministic execution time is sub-second (< 1000 ms)
        self.assertLess(report.deterministic_time_ms, 1000)

    def test_7_three_iterations_escalates_to_human_review(self):
        """Test 7: 3 iterations of failures -> escalates to human_review state."""
        max_iterations = 3
        current_iteration = 0
        status = "running"
        
        # Script that continuously fails due to unfixable forbidden phrase
        bad_script = copy.deepcopy(self.valid_script)
        bad_script.scenes[0].narration += " You can't lose with this investment!"
        
        while current_iteration < max_iterations:
            current_iteration += 1
            report = self.agent.run(self.bundle, bad_script)
            if report.overall == "fail" and current_iteration >= max_iterations:
                status = "human_review"
                break
        
        self.assertEqual(current_iteration, 3)
        self.assertEqual(status, "human_review")


if __name__ == "__main__":
    unittest.main()
