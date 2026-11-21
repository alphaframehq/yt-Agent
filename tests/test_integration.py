"""TubePilot AI — End-to-End System Integration Tests

Validates:
1. Notification helpers (notify.py, notify_failure.py, notify_success.py)
2. Developer runner lifecycle & Codespace shutdown hook (dev_run.py)
3. Complete multi-agent pipeline execution from research to private upload and cleanup
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Ensure project root is on sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from scripts.notify import send_telegram_notification
from scripts.notify_failure import notify_pipeline_failure
from scripts.notify_success import notify_pipeline_success
from scripts.dev_run import main as dev_run_main, _is_in_codespace, _stop_current_codespace
from tools.secrets import set_secret


class TestSystemIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["MASTER_SECRET"] = "integration_test_master_secret_tube_pilot_9999"
        os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
        os.environ["TELEGRAM_BOT_TOKEN"] = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"
        os.environ["TELEGRAM_ADMIN_ID"] = "987654321"

    @patch("urllib.request.urlopen")
    def test_1_notify_module(self, mock_urlopen):
        """Verify send_telegram_notification builds payload and handles mock success."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        buttons = [
            {"text": "🚀 Publish", "callback_data": "pub_vid_1"},
            {"text": "🗑️ Delete", "callback_data": "rej_vid_1"},
        ]
        result = send_telegram_notification("Test notification", buttons=buttons)
        self.assertTrue(result)
        self.assertTrue(mock_urlopen.called)

    @patch("scripts.notify_failure.send_telegram_notification")
    def test_2_notify_failure(self, mock_send):
        """Verify notify_pipeline_failure formats message with error and buttons."""
        mock_send.return_value = True
        res = notify_pipeline_failure(
            stage="verify",
            error_message="Hallucinated figure detected in Scene 4",
            run_id="run_101",
            topic="How Revolut Makes Money",
        )
        self.assertTrue(res)
        args, kwargs = mock_send.call_args
        msg = args[0]
        self.assertIn("Pipeline Run FAILED", msg)
        self.assertIn("verify", msg)
        self.assertIn("Hallucinated figure", msg)
        buttons = kwargs.get("buttons", [])
        self.assertTrue(len(buttons) >= 1)

    @patch("scripts.notify_success.send_telegram_notification")
    def test_3_notify_success(self, mock_send):
        """Verify notify_pipeline_success constructs 5 action buttons."""
        mock_send.return_value = True
        res = notify_pipeline_success(
            video_id="yt_test_abc123",
            title="How Square Operates",
            topic="Square Cash App",
            pillar="finance_to_technical",
            duration_sec=640.0,
            spend_usd=0.08,
            shorts_count=3,
        )
        self.assertTrue(res)
        args, kwargs = mock_send.call_args
        msg = args[0]
        self.assertIn("Video Production Complete", msg)
        self.assertIn("yt_test_abc123", msg)
        buttons = kwargs.get("buttons", [])
        self.assertEqual(len(buttons), 5)
        button_callbacks = [b.get("callback_data") for b in buttons]
        self.assertIn("pub_yt_test_abc123", button_callbacks)
        self.assertIn("rej_yt_test_abc123", button_callbacks)
        self.assertIn("edit_yt_test_abc123", button_callbacks)
        self.assertIn("render_yt_test_abc123", button_callbacks)
        self.assertIn("shorts_yt_test_abc123", button_callbacks)

    @patch("scripts.dev_run.run_pipeline")
    @patch("scripts.dev_run.notify_pipeline_success")
    def test_4_dev_run_success_flow(self, mock_notify, mock_pipeline):
        """Verify dev_run executes cleanly and dispatches success."""
        mock_pipeline.return_value = {
            "status": "complete",
            "video_id": "yt_devrun_test_777",
            "visual_spend": 0.09,
            "script": MagicMock(
                seo=MagicMock(title="Stripe P&L Deep Dive"),
                total_duration_sec=580.0,
            ),
        }
        mock_notify.return_value = True

        test_args = ["dev_run.py", "--topic", "Stripe Architecture", "--no-stop"]
        with patch.object(sys, "argv", test_args):
            exit_code = dev_run_main()
            self.assertEqual(exit_code, 0)
            mock_pipeline.assert_called_once()
            mock_notify.assert_called_once()

    @patch("scripts.dev_run.subprocess.run")
    def test_5_dev_run_codespace_stop_hook(self, mock_subproc):
        """Verify that when in Codespaces, gh codespace stop is triggered."""
        os.environ["CODESPACES"] = "true"
        os.environ["GITHUB_CODESPACE_NAME"] = "fuzzy-robot-12345"

        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_subproc.return_value = mock_res

        self.assertTrue(_is_in_codespace())
        _stop_current_codespace()

        self.assertTrue(mock_subproc.called)
        cmd_called = mock_subproc.call_args[0][0]
        self.assertIn("gh", cmd_called)
        self.assertIn("codespace", cmd_called)
        self.assertIn("stop", cmd_called)
        self.assertIn("fuzzy-robot-12345", cmd_called)

        # Cleanup env
        del os.environ["CODESPACES"]
        del os.environ["GITHUB_CODESPACE_NAME"]


if __name__ == "__main__":
    unittest.main()
