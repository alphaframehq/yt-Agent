"""Mandatory tests for Self-Healing Workflow (Phase 4).

Spec requires 6 specific test cases:
1. Rate limit error -> classified TRANSIENT -> retry_same
2. Provider 5xx -> classified PROVIDER_DOWN -> switch_provider
3. 401 unauthorized -> classified INVALID_KEY -> switch_key from pool
4. Daily quota 429 -> classified QUOTA_EXCEEDED -> reschedule_next_day
5. Schema fail -> classified SCHEMA_FAIL -> re_prompt
6. Unknown error -> classified FATAL -> abort
"""

import sys
import unittest
from pathlib import Path

# Add yt-agent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from workflow.errors import ErrorClass, classify_error
from workflow.healer import HealingAction, HealingContext, heal


class TestWorkflowHealer(unittest.TestCase):
    """Test suite covering the 6 mandatory error classification and healing actions."""

    def test_1_rate_limit_error_retry_same(self):
        """Test 1: Rate limit error -> classified TRANSIENT -> retry_same."""
        exc = Exception("HTTP 429: Too many requests, rate limit exceeded")
        err_class = classify_error(exc)
        self.assertEqual(err_class, ErrorClass.TRANSIENT)

        context = HealingContext(stage="script", attempt=1, max_retries=3)
        decision = heal(exc, context)
        self.assertEqual(decision.action, HealingAction.RETRY_SAME)
        self.assertGreater(decision.delay_sec, 0.0)

    def test_2_provider_5xx_switch_provider(self):
        """Test 2: Provider 5xx -> classified PROVIDER_DOWN -> switch_provider."""
        exc = Exception("HTTP 502 Bad Gateway from upstream model server")
        err_class = classify_error(exc)
        self.assertEqual(err_class, ErrorClass.PROVIDER_DOWN)

        context = HealingContext(
            stage="verify",
            current_provider="bedrock",
            current_model="claude-3-5-haiku",
            fallback_chain=[
                ["bedrock", "claude-3-5-haiku"],
                ["gemini", "gemini-3.8-flash"],
                ["openrouter", "deepseek-chat:free"],
            ],
        )
        decision = heal(exc, context)
        self.assertEqual(decision.action, HealingAction.SWITCH_PROVIDER)
        self.assertEqual(decision.provider, "gemini")
        self.assertEqual(decision.model, "gemini-3.8-flash")

    def test_3_unauthorized_switch_key(self):
        """Test 3: 401 unauthorized -> classified INVALID_KEY -> switch_key from pool."""
        exc = Exception("HTTP 401 Unauthorized: Invalid API Key provided")
        err_class = classify_error(exc)
        self.assertEqual(err_class, ErrorClass.INVALID_KEY)

        context = HealingContext(
            stage="research",
            key_name="GEMINI_KEY",
            key_pool=["AIzaSyBackupKey9876543210"],
        )
        decision = heal(exc, context)
        self.assertEqual(decision.action, HealingAction.SWITCH_KEY)
        self.assertEqual(decision.key_value, "AIzaSyBackupKey9876543210")
        self.assertTrue(decision.should_alert)

    def test_4_daily_quota_reschedule_next_day(self):
        """Test 4: Daily quota 429 -> classified QUOTA_EXCEEDED -> reschedule_next_day."""
        exc = Exception("HTTP 429: You have exceeded your daily quota for today")
        err_class = classify_error(exc)
        self.assertEqual(err_class, ErrorClass.QUOTA_EXCEEDED)

        context = HealingContext(stage="image_gen")
        decision = heal(exc, context)
        self.assertEqual(decision.action, HealingAction.RESCHEDULE_NEXT_DAY)
        self.assertEqual(decision.delay_sec, 86400.0)
        self.assertTrue(decision.should_alert)

    def test_5_schema_fail_re_prompt(self):
        """Test 5: Schema fail -> classified SCHEMA_FAIL -> re_prompt."""
        exc = Exception("pydantic.ValidationError: 1 validation error for VideoScript: missing field 'seo'")
        err_class = classify_error(exc)
        self.assertEqual(err_class, ErrorClass.SCHEMA_FAIL)

        context = HealingContext(stage="script", attempt=1, max_retries=2)
        decision = heal(exc, context)
        self.assertEqual(decision.action, HealingAction.RE_PROMPT)
        self.assertIn("re-prompting", decision.message.lower())

    def test_6_unknown_error_fatal_abort(self):
        """Test 6: Unknown error -> classified FATAL -> abort."""
        exc = RuntimeError("Fatal hardware disk failure /dev/sda unreadable")
        err_class = classify_error(exc)
        self.assertEqual(err_class, ErrorClass.FATAL)

        context = HealingContext(stage="assembly")
        decision = heal(exc, context)
        self.assertEqual(decision.action, HealingAction.ABORT)
        self.assertTrue(decision.should_alert)


if __name__ == "__main__":
    unittest.main()
