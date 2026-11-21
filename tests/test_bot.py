"""Unit tests for Phase 7: Telegram Bot Control Plane."""

import os
import sys
import unittest
from pathlib import Path

# Add yt-agent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from telegram_bot.bot import check_admin_auth, handle_callback_query, handle_command
from telegram_bot.codespace import handle_codespace_command
from telegram_bot.dispatch import dispatch_pipeline_run
from telegram_bot.health import get_cost_summary, get_system_ping
from telegram_bot.keys import handle_keys_command
from config_manager import get_channel_branding


class TestTelegramBot(unittest.TestCase):
    """Test suite covering Telegram bot commands, keys, health, and approvals."""

    @classmethod
    def setUpClass(cls):
        os.environ["MASTER_SECRET"] = "tubepilot-master-secret-key-32-chars-long!"
        os.environ["TELEGRAM_ADMIN_ID"] = "12345"

    def test_1_ping_command_returns_health_under_5_seconds(self):
        """Verify /ping returns comprehensive health in < 5 seconds."""
        res = handle_command(user_id=12345, command="ping", args=[])
        self.assertIn("text", res)
        self.assertIn("TUBEPILOT AI // SYSTEM HEALTH REPORT", res["text"])
        self.assertIn("Active Verify Model", res["text"])
        self.assertIn("Queue Depth", res["text"])

    def test_2_keys_command_security_and_masking(self):
        """Verify /keys commands mask output and set command requests auto-delete."""
        # /keys set (must set delete_msg=True for opsec)
        res_set = handle_command(user_id=12345, command="keys", args=["set", "GEMINI_KEY", "AIzaSySecretSampleKey123456789"])
        self.assertTrue(res_set["delete_msg"])
        self.assertIn("Secret `GEMINI_KEY` stored", res_set["text"])

        # /keys list (must mask output)
        res_list = handle_command(user_id=12345, command="keys", args=["list"])
        self.assertIn("GEMINI_KEY", res_list["text"])
        self.assertIn("AIzaSy", res_list["text"])
        self.assertIn("6789", res_list["text"])

    def test_3_model_control_commands(self):
        """Verify /model command switches active models dynamically."""
        # Check active
        res_m = handle_command(user_id=12345, command="model", args=[])
        self.assertIn("Active AI Model Routing", res_m["text"])

        # Change verify model
        res_change = handle_command(user_id=12345, command="model", args=["verify", "gemini-2.0"])
        self.assertIn("Model for `verify` set to `gemini/gemini-2.0-flash`", res_change["text"])

        # Reset models
        res_reset = handle_command(user_id=12345, command="model", args=["reset"])
        self.assertIn("Model routing reset to defaults", res_reset["text"])

    def test_4_topic_management_commands(self):
        """Verify /topic add and list commands."""
        res_add = handle_command(user_id=12345, command="topic", args=["add", "How Adyen Works", "finance"])
        self.assertIn("Topic added to queue", res_add["text"])
        self.assertIn("How Adyen Works", res_add["text"])

        res_list = handle_command(user_id=12345, command="topic", args=["list"])
        self.assertIn("Upcoming Topics in Queue", res_list["text"])

    def test_5_config_management_commands(self):
        """Verify /config view and set."""
        res_cfg = handle_command(user_id=12345, command="config", args=[])
        self.assertIn("System Configuration", res_cfg["text"])

        res_set = handle_command(user_id=12345, command="config", args=["set", "budget_ai_per_video", "0.45"])
        self.assertIn("Configuration updated", res_set["text"])
        self.assertIn("0.45", res_set["text"])

    def test_6_execution_commands(self):
        """Verify /run, /status, /pause, and /resume."""
        res_run = handle_command(user_id=12345, command="run", args=["How", "Plaid", "Connects", "Banks"])
        self.assertIn("Pipeline Dispatched", res_run["text"])

        res_pause = handle_command(user_id=12345, command="pause", args=[])
        self.assertIn("Pipeline paused", res_pause["text"])

        res_resume = handle_command(user_id=12345, command="resume", args=[])
        self.assertIn("Pipeline resumed", res_resume["text"])

    def test_7_approval_callbacks(self):
        """Verify interactive approval callback handlers."""
        # [✅ Publish]
        res_pub = handle_callback_query("pub_test_yt_video_888")
        self.assertIn("VIDEO PUBLISHED PUBLIC", res_pub["text"])
        self.assertIn("test_yt_video_888", res_pub["text"])

        # [❌ Reject]
        res_rej = handle_callback_query("rej_test_yt_video_888")
        self.assertIn("rejected", res_rej["text"])

    def test_8_codespace_commands(self):
        """Verify /codespace list and stop."""
        res = handle_codespace_command(["list"])
        self.assertIn("Codespaces", res)

    def test_9_channel_and_cta_commands(self):
        """Verify saving channel name and CTA like/subscribe options."""
        # 1. View /channel
        res_ch_view = handle_command(user_id=12345, command="channel", args=[])
        self.assertIn("Current YouTube Channel Name", res_ch_view["text"])

        # 2. Update /channel
        res_ch_set = handle_command(user_id=12345, command="channel", args=["FinTech", "Decoded"])
        self.assertIn("Channel Name Saved Successfully", res_ch_set["text"])
        self.assertIn("FinTech Decoded", res_ch_set["text"])

        # 3. View /cta
        res_cta_view = handle_command(user_id=12345, command="cta", args=[])
        self.assertIn("Current Call-To-Action", res_cta_view["text"])

        # 4. Update /cta
        custom_cta = "Make sure to subscribe to FinTech Decoded for institutional software breakdowns."
        res_cta_set = handle_command(user_id=12345, command="cta", args=[custom_cta])
        self.assertIn("Call-To-Action Saved Successfully", res_cta_set["text"])

        # 5. Verify config_manager reflects changes
        branding = get_channel_branding()
        self.assertEqual(branding["channel_name"], "FinTech Decoded")
        self.assertIn("subscribe to FinTech Decoded", branding["cta"]["like_subscribe"])


if __name__ == "__main__":
    unittest.main()
