"""TubePilot AI — Telegram Notification Helper

Dispatches markdown notifications, inline callback buttons, video previews,
and visual thumbnails to the Telegram Admin.
Can be imported as a module or executed directly via CLI.
"""

from __future__ import annotations

import os
import sys
import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional
import httpx
from loguru import logger

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.secrets import _get_secret_optional

__all__ = [
    "send_telegram_notification",
    "send_telegram_video",
    "send_telegram_photo",
]


def send_telegram_notification(
    text: str,
    buttons: list[dict[str, str]] | None = None,
    parse_mode: str = "Markdown",
    disable_web_page_preview: bool = False,
) -> bool:
    """Send a notification message to the Telegram admin with optional inline keyboard buttons.

    Args:
        text: The message body (Markdown format supported).
        buttons: List of dicts, each with 'text' and 'callback_data' or 'url'.
                 Example: [{'text': '🚀 Publish', 'callback_data': 'pub_123'}]
        parse_mode: 'Markdown' or 'HTML'
        disable_web_page_preview: Set False to allow rich video preview cards (default False).

    Returns:
        True if sent successfully, False otherwise.
    """
    token = _get_secret_optional("TELEGRAM_BOT_TOKEN")
    chat_id = _get_secret_optional("TELEGRAM_ADMIN_ID")

    if not token or not chat_id:
        logger.warning("Telegram credentials not configured. Notification skipped.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
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
    except urllib.error.HTTPError as e:
        if e.code == 400 and payload.get("parse_mode"):
            # Retry without parse_mode to ensure delivery even if special characters break Markdown
            payload.pop("parse_mode", None)
            try:
                retry_req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(retry_req, timeout=10) as resp2:
                    if resp2.status == 200:
                        logger.info("Telegram notification sent successfully (plain text fallback).")
                        return True
            except Exception as e2:
                logger.error(f"Telegram fallback delivery failed: {e2}")
        logger.error(f"Failed to send Telegram notification: {e}")
        return False
    except urllib.error.URLError as e:
        logger.error(f"Failed to send Telegram notification: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error sending Telegram notification: {e}")
        return False


def send_telegram_video(
    video_path: str,
    caption: str = "",
    chat_id: Optional[str | int] = None,
) -> bool:
    """Send a rendered video or preview clip directly to Telegram.

    Args:
        video_path: Absolute or relative path to the MP4 file on disk.
        caption: Optional markdown caption accompanying the video.
        chat_id: Target chat ID (defaults to TELEGRAM_ADMIN_ID).

    Returns:
        True if video was uploaded and accepted by Telegram Bot API, False otherwise.
    """
    token = _get_secret_optional("TELEGRAM_BOT_TOKEN")
    target_chat = chat_id or _get_secret_optional("TELEGRAM_ADMIN_ID")

    if not token or not target_chat:
        logger.warning("Telegram credentials not configured. Video send skipped.")
        return False

    path = Path(video_path)
    if not path.is_file():
        logger.warning(f"Cannot send video preview: File not found at '{video_path}'")
        return False

    # Check file size (Telegram Bot API limit is 50MB for upload)
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > 50.0:
        logger.warning(f"Video file ({size_mb:.1f}MB) exceeds Telegram Bot API 50MB limit.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendVideo"
    logger.info(f"Uploading video preview '{path.name}' ({size_mb:.2f}MB) to Telegram...")

    try:
        with open(path, "rb") as vf:
            files = {"video": (path.name, vf, "video/mp4")}
            data = {
                "chat_id": str(target_chat),
                "caption": caption[:1024],
                "supports_streaming": "true",
            }
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(url, data=data, files=files)
                if resp.status_code == 200:
                    logger.info(f"✅ Video preview successfully delivered to Telegram ({path.name}).")
                    return True
                else:
                    logger.warning(f"Failed to send video (HTTP {resp.status_code}): {resp.text}")
                    return False
    except Exception as e:
        logger.error(f"Exception during Telegram sendVideo: {e}")
        return False


def send_telegram_photo(
    photo_path: str,
    caption: str = "",
    chat_id: Optional[str | int] = None,
) -> bool:
    """Send an image or visual chart directly to Telegram.

    Args:
        photo_path: Absolute or relative path to the PNG/JPG file on disk.
        caption: Optional markdown caption.
        chat_id: Target chat ID (defaults to TELEGRAM_ADMIN_ID).

    Returns:
        True if uploaded successfully, False otherwise.
    """
    token = _get_secret_optional("TELEGRAM_BOT_TOKEN")
    target_chat = chat_id or _get_secret_optional("TELEGRAM_ADMIN_ID")

    if not token or not target_chat:
        return False

    path = Path(photo_path)
    if not path.is_file():
        return False

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        with open(path, "rb") as pf:
            files = {"photo": (path.name, pf, mime)}
            data = {
                "chat_id": str(target_chat),
                "caption": caption[:1024],
            }
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, data=data, files=files)
                return resp.status_code == 200
    except Exception as e:
        logger.error(f"Exception during Telegram sendPhoto: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) > 1:
        msg = " ".join(sys.argv[1:])
    else:
        msg = "🔔 TubePilot AI system notification ping."
    success = send_telegram_notification(msg)
    sys.exit(0 if success else 1)
