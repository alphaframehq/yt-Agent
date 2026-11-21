"""TubePilot AI — Telegram Notification Helper

Dispatches markdown notifications and inline callback buttons to the Telegram Admin.
Can be imported as a module or executed directly via CLI.
"""

from __future__ import annotations

import os
import sys
import json
import urllib.request
import urllib.error
from loguru import logger

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.secrets import get_secret


def send_telegram_notification(
    text: str,
    buttons: list[dict[str, str]] | None = None,
    parse_mode: str = "Markdown",
) -> bool:
    """Send a notification message to the Telegram admin with optional inline keyboard buttons.

    Args:
        text: The message body (Markdown format supported).
        buttons: List of dicts, each with 'text' and 'callback_data' or 'url'.
                 Example: [{'text': '🚀 Publish', 'callback_data': 'pub_123'}]
        parse_mode: 'Markdown' or 'HTML'

    Returns:
        True if sent successfully, False otherwise.
    """
    token = get_secret("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = get_secret("TELEGRAM_ADMIN_ID") or os.environ.get("TELEGRAM_ADMIN_ID")

    if not token or not chat_id:
        logger.warning("Telegram credentials not configured. Notification skipped.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    if buttons:
        # Group into 2 buttons per row for clean mobile layout
        keyboard_rows = []
        current_row = []
        for btn in buttons:
            btn_dict: dict = {"text": btn.get("text", "")}
            if "url" in btn:
                btn_dict["url"] = btn["url"]
            elif "callback_data" in btn:
                btn_dict["callback_data"] = btn["callback_data"]
            current_row.append(btn_dict)
            if len(current_row) >= 2:
                keyboard_rows.append(current_row)
                current_row = []
        if current_row:
            keyboard_rows.append(current_row)

        payload["reply_markup"] = {"inline_keyboard": keyboard_rows}

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                logger.info("Telegram notification sent successfully.")
                return True
            else:
                logger.warning(f"Telegram API responded with status {resp.status}")
                return False
    except urllib.error.URLError as e:
        logger.error(f"Failed to send Telegram notification: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error sending Telegram notification: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) > 1:
        msg = " ".join(sys.argv[1:])
    else:
        msg = "🔔 TubePilot AI system notification ping."
    success = send_telegram_notification(msg)
    sys.exit(0 if success else 1)
