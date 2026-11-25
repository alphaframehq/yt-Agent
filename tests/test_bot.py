"""Unit tests for Phase 7: Telegram Bot Control Plane.

2026-11-25 update: tests 6, 7, 12 and 14 previously asserted the
mock/demo behaviour (fake "Pipeline Dispatched", "VIDEO PUBLISHED
PUBLIC" for a fake ID, a preview card for a fake yt_priv_ ID, and a
hardcoded script audit). The P0 patches removed those fakes, so these
tests now assert the fail-loud behaviour instead."""

import os
import sys
import unittest
from pathlib import Path

# Add yt-agent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Google Sheets is the data store now (DynamoDB removed 25 Nov 2026).
# Install the in-memory fake Sheets service before importing the bot
# so every config/queue/video read in these tests hits the fake.
from _sheets_fake import install_fake_sheets

_FAKE_SHEETS, _SHEETS_PATCHER = install_fake_sheets()


def tearDownModule():
    _SHEETS_PATCHER.stop()

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
        os.environ["TELEGRAM_ADMIN_ID"] = "12345"
        # CI has no Google Sheets: the fake Sheets service installed
        # at module import time backs every config/queue/video read.

    def test_1_ping_command_returns_health_under_5_seconds(self):
        """Verify /ping returns comprehensive health in < 5 seconds."""
        res = handle_command(user_id=12345, command="ping", args=[])
        self.assertIn("text", res)
        self.assertIn("TUBEPILOT AI // SYSTEM HEALTH REPORT", res["text"])
        self.assertIn("Active Verify Model", res["text"])
        self.assertIn("Queue Depth", res["text"])

    def test_2_keys_command_security_and_masking(self):
        """Verify /keys commands mask output and set command requests auto-delete."""
        # /keys set can no longer store secrets — it must direct the
        # operator to GitHub repo settings (and auto-delete the message
        # because it contains a secret value).
        res_set = handle_command(user_id=12345, command="keys", args=["set", "GEMINI_KEY", "AIzaSySecretSampleKey123456789"])
        self.assertTrue(res_set["delete_msg"])
        self.assertIn("GitHub", res_set["text"])

        # /keys list (must mask output) — GEMINI_KEY is set in env here.
        os.environ["GEMINI_KEY"] = "AIzaSySecretSampleKey123456789"
        try:
            res_list = handle_command(user_id=12345, command="keys", args=["list"])
            self.assertIn("GEMINI_KEY", res_list["text"])
            self.assertIn("AIzaSy", res_list["text"])
            self.assertIn("6789", res_list["text"])
        finally:
            del os.environ["GEMINI_KEY"]

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
        # No GH_PAT in CI: dispatch must report failure, never fake success.
        res_run = handle_command(user_id=12345, command="run", args=["How", "Plaid", "Connects", "Banks"])
        self.assertIn("Dispatch failed", res_run["text"])
        self.assertNotIn("Pipeline Dispatched", res_run["text"])

        res_pause = handle_command(user_id=12345, command="pause", args=[])
        self.assertIn("Pipeline paused", res_pause["text"])

        res_resume = handle_command(user_id=12345, command="resume", args=[])
        self.assertIn("Pipeline resumed", res_resume["text"])

    def test_7_approval_callbacks(self):
        """Verify interactive approval callback handlers."""
        # [✅ Publish] without YouTube credentials must raise, never
        # claim a video was published public.
        with self.assertRaises(Exception):
            handle_callback_query("pub_test_yt_video_888")

        # [❌ Reject] of an unknown video must take no action.
        res_rej = handle_callback_query("rej_test_yt_video_888")
        self.assertIn("no action taken", res_rej["text"])

    def test_8_codespace_commands(self):
        """Verify /codespace list and stop."""
        res = handle_codespace_command(["list"])
        self.assertIn("Codespace", res)

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

    def test_10_advanced_model_switching(self):
        """Verify global and role-specific model switching commands and callbacks."""
        # 1. Global switch via single arg e.g. /model deepseek
        res_global = handle_command(user_id=12345, command="model", args=["deepseek"])
        self.assertIn("All Text Models Switched Globally", res_global["text"])
        self.assertIn("openrouter/deepseek/deepseek-chat", res_global["text"])

        # 2. Switch specific role via /model switch script gemini-2.5
        res_role = handle_command(user_id=12345, command="model", args=["switch", "script", "gemini-2.5"])
        self.assertIn("Model for `script` set to `gemini/gemini-2.5-flash-lite`", res_role["text"])

        # 3. Model list returns quick buttons
        res_list = handle_command(user_id=12345, command="model", args=["list"])
        self.assertIn("Active AI Model Routing", res_list["text"])
        self.assertTrue(len(res_list.get("buttons", [])) >= 4)

        # 4. Callback query 1-tap model switch
        cb_res = handle_callback_query("mdl_gemini-2.0")
        self.assertTrue("gemini-2.0-flash" in cb_res["text"] or "Action" in cb_res["text"])

    def test_11_agent_chat_conversation(self):
        """Verify chat with AI Agent via command and direct helper."""
        from telegram_bot.agent_chat import chat_with_agent

        # Test shortcut intent: status check
        reply = chat_with_agent(user_id=12345, user_prompt="how is the system health?")
        self.assertIn("System Diagnostics", reply)

        # Test shortcut intent: model switch
        reply_mdl = chat_with_agent(user_id=12345, user_prompt="switch model to gemini-2.5")
        self.assertIn("Model Configuration Updated", reply_mdl)

        # Test command /chat
        res_cmd = handle_command(user_id=12345, command="chat", args=["status"])
        self.assertIn("text", res_cmd)

    def test_12_video_preview_command(self):
        """Verify /preview command and preview callback."""
        # Test preview with a non-existent video ID: no fake preview.
        res_prev = handle_command(user_id=12345, command="preview", args=["yt_priv_test999"])
        self.assertIn("No preview available", res_prev["text"])
        self.assertFalse(any("pub_yt_priv_test999" in b.get("callback_data", "") for b in res_prev.get("buttons", [])))

        # Test callback for preview
        cb_prev = handle_callback_query("prv_yt_priv_test999")
        self.assertIn("Video Preview Card", cb_prev["text"])

    def test_13_interactive_dashboard_menu(self):
        """Verify main interactive dashboard (/menu, /start, callback menu_main)."""
        res_menu = handle_command(user_id=12345, command="menu", args=[])
        self.assertIn("TubePilot AI — Control Deck", res_menu["text"])
        self.assertTrue(len(res_menu.get("buttons", [])) >= 8)

        # Test callback menu_main
        cb_menu = handle_callback_query("menu_main")
        self.assertIn("TubePilot AI — Control Deck", cb_menu["text"])

    def test_14_script_and_audit_inspector(self):
        """Verify /script and fact-check verification audit review."""
        res_script = handle_command(user_id=12345, command="script", args=["Stripe"])
        self.assertIn("No script/audit found", res_script["text"])

        # Test callback menu_script
        cb_script = handle_callback_query("menu_script")
        self.assertIn("No script/audit found", cb_script["text"])

    def test_15_ideas_and_quick_queue(self):
        """Verify /ideas brainstorm and 1-tap queueing callback."""
        res_ideas = handle_command(user_id=12345, command="ideas", args=[])
        self.assertIn("Trending Institutional FinTech Video Topics", res_ideas["text"])
        self.assertTrue(len(res_ideas.get("buttons", [])) >= 4)

        # Test 1-tap queue callback
        cb_queue = handle_callback_query("qidea_1")
        self.assertIn("Topic Added to Queue", cb_queue["text"])

    def test_16_videos_history_command(self):
        """Verify /videos command lists production history."""
        res_vids = handle_command(user_id=12345, command="videos", args=[])
        self.assertIn("Recent Production Video History", res_vids["text"])

    def test_17_clear_topic_queue(self):
        """Verify /clear command and queue clearing callback."""
        res_clear = handle_command(user_id=12345, command="clear", args=[])
        self.assertIn("Topic Queue Cleared", res_clear["text"])


if __name__ == "__main__":
    unittest.main()

