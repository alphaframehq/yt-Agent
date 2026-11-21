"""TubePilot AI — Pipeline Failure Alert

Dispatches an urgent alert to the Telegram Admin when a daily or manual pipeline run fails.
Includes run metadata, failed stage, error details, and quick recovery suggestions.
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


def notify_pipeline_failure(
    stage: str = "Unknown",
    error_message: str = "Unspecified pipeline error",
    run_id: str | None = None,
    topic: str | None = None,
) -> bool:
    """Send a structured failure alert to Telegram admin.

    Args:
        stage: Pipeline stage where error occurred (e.g. 'research', 'script', 'verify', 'assembly', 'uploader')
        error_message: Error string or traceback summary
        run_id: GitHub run ID or UUID
        topic: Topic being processed

    Returns:
        True if sent successfully, False otherwise.
    """
    github_run_id = run_id or os.environ.get("GITHUB_RUN_ID", "local")
    repo = os.environ.get("GITHUB_REPOSITORY", "user/yt-agent")
    workflow_url = (
        f"https://github.com/{repo}/actions/runs/{github_run_id}"
        if github_run_id != "local"
        else "Local Run"
    )

    alert_text = (
        "🚨 *TubePilot AI — Pipeline Run FAILED*\n\n"
        f"• *Stage:* `{stage}`\n"
        f"• *Topic:* {topic or 'Scheduled Daily Run'}\n"
        f"• *Run ID:* `{github_run_id}`\n"
        f"• *Error:* ```\n{error_message[:400]}\n```\n\n"
        "💡 *Actions:* Use `/keys set` to update expired tokens, or `/run` to restart after fixing."
    )

    buttons = []
    if github_run_id != "local":
        buttons.append({"text": "📋 View GitHub Logs", "url": workflow_url})
    buttons.append({"text": "🔄 Retry Run", "callback_data": "retry_last_run"})

    logger.warning(f"Dispatching pipeline failure alert for stage: {stage}")
    return send_telegram_notification(alert_text, buttons=buttons)


if __name__ == "__main__":
    stage_arg = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PIPELINE_ERROR_STAGE", "Pipeline")
    error_arg = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("PIPELINE_ERROR_MSG", "Unknown pipeline error")
    topic_arg = sys.argv[3] if len(sys.argv) > 3 else os.environ.get("PIPELINE_TOPIC", None)

    notify_pipeline_failure(stage=stage_arg, error_message=error_arg, topic=topic_arg)
