"""TubePilot AI — Developer & Codespace Execution Runner

Runs the complete TubePilot AI pipeline locally or inside a GitHub Codespace.
Ensures that if running within GitHub Codespaces, `gh codespace stop` is ALWAYS
called in a `finally` block to protect against runaway billing.
"""

from __future__ import annotations

import os
import sys
import argparse
import subprocess
from loguru import logger

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except ImportError:
    pass

from config_manager import snapshot_config
from orchestration.graph import run_pipeline
from db.dynamo import dequeue_next_topic, get_topic_queue
from scripts.notify_success import notify_pipeline_success
from scripts.notify_failure import notify_pipeline_failure


def _is_in_codespace() -> bool:
    """Check if execution environment is a GitHub Codespace."""
    return os.environ.get("CODESPACES") == "true" or bool(os.environ.get("GITHUB_CODESPACE_NAME"))


def _stop_current_codespace() -> None:
    """Safely shut down current GitHub Codespace to prevent idle charges."""
    cs_name = os.environ.get("GITHUB_CODESPACE_NAME")
    logger.info(f"Initiating automatic Codespace shutdown for: {cs_name or 'current'}...")
    try:
        cmd = ["gh", "codespace", "stop"]
        if cs_name:
            cmd.extend(["-c", cs_name])
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            logger.info("Codespace stopped successfully.")
        else:
            logger.warning(f"gh codespace stop returned code {res.returncode}: {res.stderr}")
    except Exception as e:
        logger.warning(f"Could not automatically stop Codespace: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="TubePilot AI Local & Codespace Pipeline Runner")
    parser.add_argument("--topic", type=str, default=None, help="Video topic to research and generate")
    parser.add_argument(
        "--pillar",
        type=str,
        default="finance_to_technical",
        choices=["finance_to_technical", "tech_to_financial"],
        help="Content pillar (default: finance_to_technical)",
    )
    parser.add_argument(
        "--no-stop",
        action="store_true",
        help="Disable automatic Codespace shutdown on exit",
    )
    args = parser.parse_args()

    topic = args.topic
    pillar = args.pillar

    # If no topic specified, try popping from DynamoDB queue
    if not topic:
        queued = dequeue_next_topic()
        if queued:
            topic = queued.get("topic")
            pillar = queued.get("pillar", pillar)
            logger.info(f"Dequeued topic from DynamoDB queue: '{topic}' [{pillar}]")
        else:
            topic = "How Stripe Makes Money"
            logger.info(f"Queue empty. Using default development topic: '{topic}' [{pillar}]")

    logger.info(f"=== Starting TubePilot AI Pipeline Run ===")
    logger.info(f"Topic: '{topic}' | Pillar: '{pillar}'")

    exit_code = 0
    try:
        cfg = snapshot_config()
        result = run_pipeline(topic=topic, pillar=pillar, config=cfg)

        status = result.get("status")
        video_id = result.get("video_id")
        script = result.get("script")

        if status == "complete" and video_id:
            logger.info(f"✅ Pipeline succeeded! YouTube Video ID: {video_id}")
            rendered_video = result.get("rendered_video_path")
            rendered_shorts = result.get("rendered_shorts_path")
            if rendered_video:
                logger.info(f"🎬 Longform Video File: {rendered_video}")
            if rendered_shorts:
                logger.info(f"📱 Shorts Video File: {rendered_shorts}")
            title = getattr(script.seo, "title", f"{topic} Breakdown") if script else topic
            duration = getattr(script, "total_duration_sec", 600.0) if script else 600.0
            spend = result.get("visual_spend", 0.09)

            notify_pipeline_success(
                video_id=video_id,
                title=title,
                topic=topic,
                pillar=pillar,
                duration_sec=duration,
                spend_usd=spend,
                shorts_count=3,
                video_path=rendered_shorts or rendered_video,
                thumbnail_path=result.get("thumbnail_path"),
            )
        else:
            error_msg = f"Pipeline ended with status: '{status}'. Halted for review."
            logger.warning(f"⚠️ {error_msg}")
            notify_pipeline_failure(
                stage="orchestration",
                error_message=error_msg,
                topic=topic,
            )
            exit_code = 1

    except Exception as e:
        logger.error(f"❌ Fatal error in dev_run: {e}")
        notify_pipeline_failure(
            stage="pipeline_execution",
            error_message=str(e),
            topic=topic,
        )
        exit_code = 2
    finally:
        if _is_in_codespace() and not args.no_stop:
            _stop_current_codespace()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
