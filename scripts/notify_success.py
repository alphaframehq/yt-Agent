"""TubePilot AI — Pipeline Success Notification

Dispatches a rich video review notification to the Telegram Admin upon completion.
Includes private YouTube link, video metadata, 3 Shorts status, and action buttons.
"""

from __future__ import annotations

import os
import sys
from loguru import logger

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from scripts.notify import send_telegram_notification


def notify_pipeline_success(
    video_id: str,
    title: str,
    topic: str,
    pillar: str,
    duration_sec: float,
    spend_usd: float,
    shorts_count: int = 3,
) -> bool:
    """Send a structured success notification with review and action buttons.

    Args:
        video_id: YouTube private video ID
        title: YouTube video title
        topic: Research topic
        pillar: Content pillar ('finance_to_technical' or 'tech_to_financial')
        duration_sec: Video length in seconds
        spend_usd: Total AI generative spend for this run
        shorts_count: Number of shorts generated

    Returns:
        True if sent successfully, False otherwise.
    """
    yt_url = f"https://youtu.be/{video_id}"
    mins = int(duration_sec // 60)
    secs = int(duration_sec % 60)

    msg = (
        "🎬 *TubePilot AI — Video Production Complete!*\n\n"
        f"• *Title:* {title}\n"
        f"• *Topic:* {topic}\n"
        f"• *Pillar:* `{pillar}`\n"
        f"• *Duration:* {mins}:{secs:02d}\n"
        f"• *AI Spend:* ${spend_usd:.2f}\n"
        f"• *Status:* 🔒 Uploaded as PRIVATE\n"
        f"• *Shorts Generated:* {shorts_count} vertical clips ready\n\n"
        f"🔗 [Watch Private Preview]({yt_url})\n\n"
        "Tap below to take action:"
    )

    buttons = [
        {"text": "🚀 Publish Public", "callback_data": f"pub_{video_id}"},
        {"text": "🗑️ Reject & Delete", "callback_data": f"rej_{video_id}"},
        {"text": "✏️ Re-Edit Script", "callback_data": f"edit_{video_id}"},
        {"text": "🔄 Re-Render", "callback_data": f"render_{video_id}"},
        {"text": "📱 Post Shorts", "callback_data": f"shorts_{video_id}"},
    ]

    logger.info(f"Dispatching success notification for video: {video_id}")
    return send_telegram_notification(msg, buttons=buttons)


if __name__ == "__main__":
    vid = sys.argv[1] if len(sys.argv) > 1 else "demo_vid_001"
    vtitle = sys.argv[2] if len(sys.argv) > 2 else "How Stripe Makes Money (P&L Breakdown)"
    vtopic = sys.argv[3] if len(sys.argv) > 3 else "Stripe Financial Architecture"
    vpillar = sys.argv[4] if len(sys.argv) > 4 else "finance_to_technical"
    dur = float(sys.argv[5]) if len(sys.argv) > 5 else 620.0
    spend = float(sys.argv[6]) if len(sys.argv) > 6 else 0.09
    shorts = int(sys.argv[7]) if len(sys.argv) > 7 else 3

    notify_pipeline_success(
        video_id=vid,
        title=vtitle,
        topic=vtopic,
        pillar=vpillar,
        duration_sec=dur,
        spend_usd=spend,
        shorts_count=shorts,
    )
