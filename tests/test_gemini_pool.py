"""TubePilot AI — Gemini Multi-Key Failover Pool Unit Tests

Validates:
1. Dynamic pool key discovery (GEMINI_KEY, GEMINI_KEY_POOL, GEMINI_KEY_1..N)
2. Cooldown tracking, active key selection, and rotation
3. LLMProvider seamless failover on HTTP 429 / 401 / 403
4. Imagen 3 image generation key pool retry
5. Telegram Bot /keys pool commands (list, add, rotate)
"""

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.secrets import (
    add_gemini_pool_key,
    get_active_gemini_key,
    get_gemini_pool_keys,
    get_gemini_pool_status,
    mark_gemini_key_failed,
    rotate_gemini_key,
    reset_gemini_pool,
    set_secret,
)
from tools.llm_provider import LLMProvider
from tools.image_gen import _generate_imagen3
from telegram_bot.keys import handle_keys_command


class TestGeminiKeyPool(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["MASTER_SECRET"] = "gemini_pool_unit_test_master_secret_8888"
        os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

    def setUp(self):
        reset_gemini_pool()
        # Configure a pool of 3 Gemini keys
        set_secret("GEMINI_KEY", "AIzaSyPrimary001_AABBCCDDEEFFGG11")
        set_secret("GEMINI_KEY_1", "AIzaSyStandby002_HHIIJJKKLLMMNN22")
        set_secret("GEMINI_KEY_2", "AIzaSyReserve003_OOPPQQRRSSTTUU33")


    def test_1_pool_discovery(self):
        """Verify all configured Gemini keys are discovered and deduplicated."""
        keys = get_gemini_pool_keys()
        self.assertGreaterEqual(len(keys), 3)
        self.assertIn("AIzaSyPrimary001_AABBCCDDEEFFGG11", keys)
        self.assertIn("AIzaSyStandby002_HHIIJJKKLLMMNN22", keys)
        self.assertIn("AIzaSyReserve003_OOPPQQRRSSTTUU33", keys)

    def test_2_cooldown_and_active_key_rotation(self):
        """Verify key failure places it into cooldown and selects next healthy key."""
        key1 = "AIzaSyPrimary001_AABBCCDDEEFFGG11"
        key2 = "AIzaSyStandby002_HHIIJJKKLLMMNN22"

        active_before = get_active_gemini_key()
        self.assertEqual(active_before, key1)

        # Mark key1 failed with 429
        next_key = mark_gemini_key_failed(key1, error_status=429, cooldown_sec=15.0)
        self.assertEqual(next_key, key2)

        # Verify pool status reports cooldown
        status = get_gemini_pool_status()
        key1_status = next(s for s in status if s["name"] == "GEMINI_KEY")
        self.assertEqual(key1_status["status"], "cooldown")
        self.assertGreater(key1_status["cooldown_remaining_sec"], 0)

        # Active key is now key2
        self.assertEqual(get_active_gemini_key(), key2)

    def test_3_manual_rotation(self):
        """Verify rotate_gemini_key advances active index."""
        k1 = get_active_gemini_key()
        k2 = rotate_gemini_key()
        self.assertNotEqual(k1, k2)

    @patch("httpx.Client.post")
    def test_4_llm_provider_failover_on_429(self, mock_post):
        """Verify LLMProvider._call_gemini transparently retries on key 2 when key 1 hits 429."""
        # 1st attempt: 429 Rate Limit
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.request = MagicMock()

        # 2nd attempt: 200 OK
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "candidates": [
                {"content": {"parts": [{"text": "Gemini answer via failover key"}]}}
            ]
        }

        mock_post.side_effect = [resp_429, resp_200]

        provider = LLMProvider("gemini", "gemini-2.0-flash")
        res = provider.call("system prompt", "user prompt")

        self.assertEqual(res.content, "Gemini answer via failover key")
        self.assertEqual(res.provider, "gemini")
        self.assertEqual(mock_post.call_count, 2)

    @patch("httpx.Client.post")
    def test_5_imagen3_pool_failover(self, mock_post):
        """Verify _generate_imagen3 retries on key 2 when key 1 hits 429."""
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.request = MagicMock()

        # Valid 1x1 PNG base64
        tiny_png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "predictions": [{"bytesBase64Encoded": tiny_png}]
        }

        mock_post.side_effect = [resp_429, resp_200]

        dest_file = Path("cache/images/test_pool_imagen.png")
        dest_file.parent.mkdir(parents=True, exist_ok=True)

        res_path = _generate_imagen3("test prompt", dest_file)
        self.assertTrue(res_path.exists())
        self.assertEqual(mock_post.call_count, 2)

        # Cleanup test file
        if dest_file.exists():
            dest_file.unlink()

    def test_6_telegram_keys_pool_commands(self):
        """Verify Telegram /keys pool list, add, and rotate commands."""
        # /keys pool list
        res_list = handle_keys_command(["pool", "list"])
        self.assertIn("Gemini API Key Pool", res_list["text"])
        self.assertFalse(res_list["delete_caller_msg"])

        # /keys pool add gemini NEW_KEY
        new_key = "AIzaSyFourthKey999_ZZYYXXWWVVUU44"
        res_add = handle_keys_command(["pool", "add", "gemini", new_key])
        self.assertIn("Added Gemini API key to pool", res_add["text"])
        self.assertTrue(res_add["delete_caller_msg"])  # opsec auto-delete

        # /keys pool rotate gemini
        res_rotate = handle_keys_command(["pool", "rotate", "gemini"])
        self.assertIn("Manually rotated Gemini pool", res_rotate["text"])


if __name__ == "__main__":
    unittest.main()
