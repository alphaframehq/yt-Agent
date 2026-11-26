# LOCAL PRODUCTION PATCH — 2026-11-25
# Source: alphaframehq/yt-Agent main @ c39f42541b8e784dcf41c09583dc57227be07667 — scripts/dev_run.py
# Fixes: P0 audit #27 (task #27 cron): scheduled daily run (--scheduled) with an empty Google Sheets queue exits with 'queue empty, nothing to run' (exit 0) instead of defaulting to 'How Stripe Makes Money'; the default topic is used only with explicit --allow-default-topic; hardcoded success-notify values removed — duration from script.meta.estimated_duration_sec, spend from the real pipeline result, shorts_count from actually rendered Shorts (unknown = 0.0, never 600.0/0.09/3).
# Applied via PR #1 (branch fix/production-mocks), 2026-11-25.
# No secrets in this file — env placeholders / env-var names only.

"""TubePilot AI — Developer & Codespace Execution Runner

Runs the complete TubePilot AI pipeline locally or inside a GitHub Codespace.
Ensures that if running within GitHub Codespaces, `gh codespace stop` is ALWAYS
called in a `finally` block to protect against runaway billing.

Scheduled (cron) runs: an empty Google Sheets queue exits with
"queue empty, nothing to run" — never a default topic. A default topic
is used only for an explicit local dev run (--allow-default-topic).
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
from db.sheets import dequeue_next_topic, update_topic_status
from tools.topic_filter import check_topic
from scripts.notify_success import notify_pipeline_success
from scripts.notify_failure import notify_pipeline_failure

# Max queued topics attempted in one invocation. Each attempt dequeues
# (and marks running/failed), so the loop always makes progress and
# cannot spin forever on a poisoned queue.
_MAX_TOPICS_PER_RUN = 5

# Substrings (lowercased) in a pipeline error that mean the TOPIC
# itself is unproduceable — not an infra failure. Such a failure
# marks the topic failed in the Sheet and the runner moves on to the
# next queued topic instead of alerting/aborting the whole run.
_TOPIC_FAILURE_MARKERS: tuple[str, ...] = (
    "no public ticker",
    "topic rejected",
    "topic filter",
    "blocked-term",
    "off-niche",
    "safety",
    "prompt was blocked",
    "blockreason",
)


def _is_topic_failure(error: str | None) -> bool:
    """Return True when *error* blames the topic, not the infrastructure."""
    if not error:
        return False
    lowered = str(error).lower()
    return any(marker in lowered for marker in _TOPIC_FAILURE_MARKERS)


def _mark_topic_failed(topic_id: str | None) -> bool:
    """Best-effort: mark one queued topic ``failed`` in Google Sheets.

    Never raises — a Sheet write failure must not crash the run on
    top of the topic failure it is recording.
    """
    if not topic_id:
        logger.warning("Cannot mark topic failed: no topic_id on the queued row.")
        return False
    try:
        return update_topic_status(topic_id, "failed")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not mark topic {topic_id} failed in Sheets: {e}")
        return False


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


def _run_single_topic(
    topic: str, pillar: str, args: argparse.Namespace, topic_id: str | None = None
) -> tuple[int, bool]:
    """Run the pipeline once for *topic*.

    Returns ``(exit_code, topic_failed)``. ``topic_failed`` is True
    only when the failure is attributable to the topic itself (safety
    refusal, off-niche / unresearchable topic) — the caller then
    marks the topic failed (already done here) and moves on to the
    next queued topic. Any other failure returns ``topic_failed=False``
    and the caller must stop the run and alert.
    """
    logger.info("=== Starting TubePilot AI Pipeline Run ===")
    logger.info(f"Topic: '{topic}' | Pillar: '{pillar}'")

    try:
        cfg = snapshot_config()
        result = run_pipeline(topic=topic, pillar=pillar, config=cfg)
    except Exception as e:
        err = str(e)
        logger.error(f"❌ Fatal error in dev_run: {e}")
        if _is_topic_failure(err):
            logger.warning(
                "Failure is topic-attributable; marking topic failed "
                "in the Sheet and moving on to the next queued topic."
            )
            _mark_topic_failed(topic_id)
            return 1, True
        notify_pipeline_failure(
            stage="pipeline_execution",
            error_message=err,
            topic=topic,
        )
        return 2, False

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
        # Real values only: duration from the script model, spend
        # from the pipeline result, shorts actually rendered.
        # Unknown duration/spend is reported as 0.0 (unknown), never
        # as a fabricated 600.0 / 0.09.
        duration = None
        if script is not None:
            meta = getattr(script, "meta", None)
            duration = getattr(meta, "estimated_duration_sec", None) if meta is not None else None
            if duration is None:
                duration = getattr(script, "total_duration_sec", None)
        if duration is None:
            logger.warning("Script duration unknown; reporting duration_sec=0.0 (unknown)")
            duration = 0.0
        spend = result.get("visual_spend")
        if spend is None:
            spend = 0.0
        shorts_paths = result.get("rendered_shorts_paths") or (
            [rendered_shorts] if rendered_shorts else []
        )
        shorts_count = len(shorts_paths)

        notify_pipeline_success(
            video_id=video_id,
            title=title,
            topic=topic,
            pillar=pillar,
            duration_sec=duration,
            spend_usd=spend,
            shorts_count=shorts_count,
            video_path=rendered_shorts or rendered_video,
            thumbnail_path=result.get("thumbnail_path"),
        )
        return 0, False

    # Not complete: distinguish a bad topic (skip it, next topic)
    # from a real run failure (alert and stop).
    error_msg = result.get("error") or (
        f"Pipeline ended with status: '{status}'. Halted for review."
    )
    if _is_topic_failure(error_msg):
        logger.warning(
            f"⚠️ Topic '{topic}' failed as unproduceable ({error_msg}); "
            "marking failed in the Sheet and moving on to the next queued topic."
        )
        _mark_topic_failed(topic_id)
        return 1, True

    logger.warning(f"⚠️ Pipeline ended with status: '{status}'. Halted for review.")
    notify_pipeline_failure(
        stage="orchestration",
        error_message=error_msg,
        topic=topic,
    )
    return 1, False


def main() -> int:
    parser = argparse.ArgumentParser(description="TubePilot AI Local & Codespace Pipeline Runner")
    parser.add_argument("--topic", type=str, default=None, help="Video topic to research and generate")
    parser.add_argument(
        "--pillar",
        type=str,
        # 26 Nov 2026 niche correction: the channel is AI — anything
        # new in AI (ai_everything). The two finance pillars are kept
        # as accepted values so older queued rows / bot callers do not
        # break, but nothing new should use them.
        default="ai_everything",
        choices=["ai_everything", "finance_to_technical", "tech_to_financial"],
        help="Content pillar (default: ai_everything)",
    )
    parser.add_argument(
        "--no-stop",
        action="store_true",
        help="Disable automatic Codespace shutdown on exit",
    )
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Scheduled (cron) run: exit cleanly with 'queue empty, nothing to run' when the Google Sheets queue is empty instead of using a default topic",
    )
    parser.add_argument(
        "--allow-default-topic",
        action="store_true",
        help="Explicit local-dev opt-in only: use the default dev topic when no --topic is given and the queue is empty",
    )
    args = parser.parse_args()

    topic = args.topic
    pillar = args.pillar

    def _maybe_stop_codespace() -> None:
        if _is_in_codespace() and not args.no_stop:
            _stop_current_codespace()

    # --- Explicit --topic: validate it up front; a blocked/off-niche
    # explicit topic fails loudly (exit 2) — there is no "next topic"
    # to fall through to.
    if topic:
        allowed, reason = check_topic(topic, pillar)
        if not allowed:
            logger.error(
                f"Explicit topic rejected by topic filter: '{topic}' ({reason})"
            )
            _maybe_stop_codespace()
            return 2
        try:
            exit_code, _ = _run_single_topic(topic, pillar, args)
            return exit_code
        finally:
            _maybe_stop_codespace()

    # --- Queue path (26 Nov 2026): dequeue in a loop until one topic
    # succeeds, the queue empties, or the per-run cap is hit. A bad
    # topic is marked failed in the Sheet and skipped; an empty queue
    # exits 0 ("queue empty, nothing to do") — never a default topic,
    # never a crash, with or without --scheduled. (The daily workflow
    # runs without --scheduled, so the old code returned 2 here and
    # the retry wrapper re-ran a run that had nothing to do.)
    try:
        for _attempt in range(_MAX_TOPICS_PER_RUN):
            try:
                queued = dequeue_next_topic()
            except Exception:
                logger.exception("Failed to dequeue next topic from Google Sheets")
                notify_pipeline_failure(
                    stage="dequeue",
                    error_message="Google Sheets dequeue failed; see runner logs.",
                    topic=None,
                )
                return 2
            if not queued or not queued.get("topic"):
                if args.allow_default_topic:
                    # Explicit local-dev opt-in only.
                    default_topic = "How LLMs Are Trained"
                    logger.info(
                        f"Queue empty. Explicit dev default topic: '{default_topic}' [{pillar}]"
                    )
                    exit_code, _ = _run_single_topic(default_topic, pillar, args)
                    return exit_code
                logger.info("queue empty, nothing to do")
                return 0

            q_topic = queued.get("topic")
            q_pillar = queued.get("pillar") or pillar
            if q_pillar not in ("ai_everything", "finance_to_technical", "tech_to_financial"):
                logger.warning(
                    f"Queued topic has invalid pillar {q_pillar!r}; using '{pillar}' instead."
                )
                q_pillar = pillar
            topic_id = queued.get("topic_id")

            allowed, reason = check_topic(q_topic, q_pillar)
            if not allowed:
                logger.warning(
                    f"Skipping queued topic '{q_topic}' ({reason}) — "
                    "marking failed so it is never dequeued again."
                )
                _mark_topic_failed(topic_id)
                continue

            logger.info(f"Dequeued topic from Google Sheets queue: '{q_topic}' [{q_pillar}]")
            exit_code, topic_failed = _run_single_topic(
                q_topic, q_pillar, args, topic_id=topic_id
            )
            if exit_code == 0:
                return 0
            if topic_failed:
                continue
            return exit_code

        logger.error(
            f"No topic produced a video after {_MAX_TOPICS_PER_RUN} queued "
            "topics; failure alerts were sent for the non-topic failures."
        )
        return 1
    finally:
        _maybe_stop_codespace()


if __name__ == "__main__":
    sys.exit(main())
