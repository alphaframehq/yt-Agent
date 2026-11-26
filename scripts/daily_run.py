"""TubePilot AI — Daily Runner: 1 long video + 3 Shorts per day.

Runs the full daily output for ONE niche (``ai_everything`` — AI,
anything new in AI: new tools, projects, discoveries, inventions,
AI scams/fraud, predictions, strategy, formulas, ML/LLM training,
pros/cons, leaks, conspiracy; channel "Alpha Frame"):

    research -> trend research -> long-form script -> verify/fix
    -> long video assemble + private upload
    -> 3 vertical Shorts (same day's topic/research) assemble + upload

Flow (``scripts/dev_run.py`` topic resolution, unchanged):

1. Topic comes from ``--topic`` or, before dequeue, from the daily
   trending-topic picker (``tools/trends.pick_daily_trends``): if
   today has a strong trending AI topic that passes the topic-safety
   filter and is not already queued, it is enqueued at priority 2
   and preferred over the stale base queue — the queued 60 topics
   stay as the base queue and are the fallback. Otherwise the topic
   is dequeued from the Google Sheets queue (``yt_topics_queue``).
   A dequeued topic is checked with the topic-safety/niche filter
   first: a rejected topic is marked ``failed`` in the Sheet and
   the next queued topic is tried (up to 5 per run), so one
   poisoned row can never fail the whole day. With ``--scheduled``
   and an empty queue (and no strong trend), exits 0 with "queue
   empty, nothing to run" — never a default topic. A default topic
   is used only with explicit ``--allow-default-topic`` (local dev).
2. The long pipeline runs via ``orchestration.graph.run_pipeline``
   (research, trends, script, verify, finalize). The trend step is
   non-fatal; a trends failure never blocks the long video.
3. Shorts run AFTER the long pipeline, from the same day's research
   bundle (``agents/shorts.render_and_upload_shorts``), reusing the
   Shorts already rendered by ``node_finalize`` when available.
   Isolation: each Short uploads independently; a Short's failure
   is logged + recorded in ``yt_shorts`` and never changes the long
   video's exit code, and a long-video failure never prevents the
   Shorts attempt (Shorts run whenever a research bundle exists).
4. Exit code reflects the LONG video only: 0 = long video complete
   (even if some Shorts failed — their outcome is in the log, the
   Sheet and the Telegram notify), 1 = long video failed, 2 = no
   topic / fatal runner error. The Telegram success notify carries
   the real uploaded-Shorts count.

Called by ``.github/workflows/daily.yml`` (04:00 UTC daily) as:
``python -u scripts/daily_run.py --scheduled --pillar
ai_everything --shorts-count 3 --no-stop``.
"""

from __future__ import annotations

import os
import sys
import argparse
import subprocess
from typing import Any, Dict, List, Optional

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

from agents.shorts import SHORTS_PER_DAY, render_and_upload_shorts, summarize_shorts
from agents.uploader import cross_link_daily_uploads
from config_manager import snapshot_config
from db.sheets import (
    dequeue_next_topic,
    enqueue_topic,
    list_topics_queue,
    record_pipeline_run,
    save_trend_report,
)
from orchestration.graph import run_pipeline
from scripts.dev_run import (
    _MAX_TOPICS_PER_RUN,
    _is_topic_failure,
    _mark_topic_failed,
)
from scripts.notify_success import notify_pipeline_success
from scripts.notify_failure import notify_pipeline_failure
from tools.topic_filter import check_topic
from tools.trends import pick_daily_trends, trend_report_for_pick

# Priority for a trending-picked topic enqueued by
# _prefer_trending_topic: above the base queue's default 1, so the
# normal dequeue returns it first. (See db/sheets.enqueue_topic /
# get_next_topic: highest priority wins, then oldest added_at.)
_TRENDING_PRIORITY = 2


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


def _prefer_trending_topic(args: argparse.Namespace) -> Optional[str]:
    """Daily trending-topic preference — runs BEFORE dequeue.

    Fetches today's strongest trending AI topic(s) from the free
    keyless RSS sources (``tools/trends.pick_daily_trends``) and, if
    the best pick is strong, passes the topic-safety filter
    (``tools/topic_filter.check_topic`` — the same gate the pipeline
    entry applies) and is not already in the queue, enqueues it at
    priority 2 so the dequeue right after this returns it first, and
    logs the day's trends + sources to the ``yt_trends`` Sheet tab
    (best-effort: a Sheets failure never blocks the run).

    Returns the preferred topic string, or None to fall back to the
    normal queued dequeue. Never raises: a picker failure, a
    rejected pick, an already-queued pick, or a Sheets failure all
    fall back silently to the queue. Explicit ``--topic`` bypasses
    this entirely (handled in ``_resolve_topic`` before this call).
    """
    if getattr(args, "no_trending", False):
        logger.info("Trending preference disabled (--no-trending); using queued topic.")
        return None
    try:
        pick = pick_daily_trends()
    except Exception as exc:  # noqa: BLE001 — picker must never kill the day
        logger.warning(f"Trending picker failed ({exc}); falling back to queued topics.")
        return None
    if not pick.has_pick or pick.best_topic is None:
        logger.info(
            "No strong trending AI topic today "
            f"(sources: {pick.sources or 'none'}; errors: {pick.errors or 'none'}); "
            "using queued topic."
        )
        return None
    topic = pick.best_topic.topic
    allowed, reason = check_topic(topic, args.pillar)
    if not allowed:
        logger.warning(
            f"Trending topic '{topic}' rejected by topic filter ({reason}); "
            "using queued topic."
        )
        return None
    try:
        already_queued = any(
            (row.get("topic") or "").strip().lower() == topic.strip().lower()
            for row in list_topics_queue()
        )
    except Exception as exc:  # noqa: BLE001 — Sheets read failed: proceed without the check
        logger.warning(f"Could not list queue to de-dupe trending topic ({exc}); proceeding.")
        already_queued = False
    if already_queued:
        logger.info(
            f"Trending topic '{topic}' is already in the queue; "
            "using normal dequeue order."
        )
        return None
    try:
        enqueue_topic(topic, pillar=args.pillar, priority=_TRENDING_PRIORITY)
    except Exception as exc:  # noqa: BLE001 — Sheets write failed: fall back to queue
        logger.warning(f"Could not enqueue trending topic '{topic}' ({exc}); using queued topic.")
        return None
    try:
        save_trend_report(topic, trend_report_for_pick(pick, topic), pillar=args.pillar)
    except Exception as exc:  # noqa: BLE001 — Sheet unavailable: log, continue
        logger.warning(f"Could not save trending pick to yt_trends ({exc}); continuing.")
    logger.info(
        f"Trending topic preferred for today: '{topic}' "
        f"(score {pick.best_topic.score}; sources: {pick.best_topic.sources})"
    )
    return topic


_UNSET = object()


def _resolve_queued_topic(
    args: argparse.Namespace, trending: object = _UNSET
) -> Optional[tuple]:
    """Resolve the next usable queued topic.

    Returns ``(topic, pillar, topic_id)`` or None when a scheduled
    run has an empty queue (caller exits 0). Raises SystemExit(2)
    when no topic can be resolved for a non-scheduled run, and
    SystemExit(1) when every dequeued topic was rejected by the
    topic filter (all are marked failed, so the queue has no usable
    topic left). Explicit --topic returns immediately with
    topic_id None (no fall-through to the queue).

    A dequeued topic is checked with the topic-safety/niche filter
    BEFORE any pipeline work (same gate as scripts/dev_run.py and
    the pipeline entry): a rejected topic is marked failed in the
    Sheet and the next queued topic is tried, so one poisoned row
    (e.g. run #13's 'best poses for sex', priority 10) can never
    fail the whole day again. ``trending`` is the topic string
    _prefer_trending_topic already enqueued (or None); pass it in
    so the picker runs once per run, not once per dequeue attempt.
    """
    if args.topic:
        return (args.topic, args.pillar, None)
    if trending is _UNSET:
        trending = _prefer_trending_topic(args)
    for _attempt in range(_MAX_TOPICS_PER_RUN):
        queued = dequeue_next_topic()
        if not queued or not queued.get("topic"):
            if args.scheduled:
                logger.info("queue empty, nothing to run")
                return None
            if args.allow_default_topic:
                logger.info("Queue empty. Explicit dev default topic: 'How Stripe Makes Money'")
                return ("How Stripe Makes Money", args.pillar, None)
            logger.error(
                "No topic supplied and the Google Sheets queue is empty. "
                "Pass --topic, queue a topic, or use --allow-default-topic "
                "for an explicit local dev run."
            )
            raise SystemExit(2)
        topic = queued.get("topic")
        q_pillar = queued.get("pillar") or args.pillar
        if q_pillar not in ("ai_everything", "finance_to_technical", "tech_to_financial"):
            logger.warning(
                f"Queued topic has invalid pillar {q_pillar!r}; using '{args.pillar}' instead."
            )
            q_pillar = args.pillar
        topic_id = queued.get("topic_id")
        allowed, reason = check_topic(topic, q_pillar)
        if not allowed:
            logger.warning(
                f"Skipping queued topic '{topic}' ({reason}) — "
                "marking failed so it is never dequeued again."
            )
            _mark_topic_failed(topic_id)
            continue
        if trending and topic.strip().lower() == trending.strip().lower():
            logger.info(f"Dequeued trending topic for today: '{topic}' [{q_pillar}]")
        elif trending:
            # A stale higher-priority row won the dequeue over the
            # freshly enqueued trend (e.g. an older trending pick
            # still pending at priority 2). The dequeued topic is
            # still a valid queued topic — run it; the fresh trend
            # stays pending for the next dequeue. Follow-up (needs a
            # db/sheets.py change, out of scope here): expire stale
            # trending rows so they cannot crowd out the base queue.
            logger.warning(
                f"Trending topic '{trending}' was enqueued but dequeue "
                f"returned '{topic}' (older higher-priority row); "
                "running the dequeued topic."
            )
        else:
            logger.info(f"Dequeued topic from Google Sheets queue: '{topic}' [{q_pillar}]")
        return (topic, q_pillar, topic_id)
    logger.error(
        f"No usable topic after {_MAX_TOPICS_PER_RUN} queued topics "
        "(all rejected by the topic filter and marked failed)."
    )
    raise SystemExit(1)


def _resolve_topic(args: argparse.Namespace) -> Optional[str]:
    """Resolve the day's topic (same rules as scripts/dev_run.py).

    Returns the topic, or None when a scheduled run has an empty
    queue (caller exits 0). Raises SystemExit(2) when no topic can
    be resolved for a non-scheduled run. Thin wrapper over
    _resolve_queued_topic, which also returns the pillar/topic_id
    the daily loop needs to mark a failed topic.
    """
    resolved = _resolve_queued_topic(args)
    return resolved[0] if resolved is not None else None


def run_daily(
    topic: str,
    pillar: str = "ai_everything",
    shorts_count: int = SHORTS_PER_DAY,
) -> Dict[str, Any]:
    """Run one day: long video + ``shorts_count`` Shorts.

    Returns a summary dict with the long-video result, the per-Short
    results, and the exit code the CLI should use. Never raises for
    a Shorts failure — Shorts outcomes are data, not exceptions.
    """
    logger.info(f"=== Starting TubePilot AI Daily Run ===")
    logger.info(f"Topic: '{topic}' | Pillar: '{pillar}' | Shorts: {shorts_count}")

    cfg = snapshot_config()
    result = run_pipeline(topic=topic, pillar=pillar, config=cfg)
    state = result.get("state")
    status = result.get("status")
    video_id = result.get("video_id")
    script = result.get("script")
    long_ok = status == "complete" and bool(video_id)

    # --- Shorts: same day's topic/research, independent of the long outcome.
    short_results: List[Dict[str, Any]] = []
    shorts_summary: Dict[str, Any] = {"overall": "skipped", "uploaded": 0, "failed": 0, "skipped": 0, "video_ids": [], "errors": []}
    bundle = result.get("research")
    if shorts_count > 0 and bundle is not None:
        rendered = None
        if state is not None:
            paths = list(getattr(state, "rendered_shorts_paths", []) or [])
            scripts = list(getattr(state, "shorts_scripts", []) or [])
            if paths and len(paths) == len(scripts):
                rendered = list(zip(paths, scripts))
        try:
            # The long video's ID is known by now (it uploaded first),
            # so each Short's description links it, and the bundle adds
            # the Sources block to each Short.
            long_video = None
            if long_ok and script is not None:
                long_video = {
                    "title": getattr(script.seo, "title", topic),
                    "yt_video_id": video_id,
                    "yt_url": f"https://youtu.be/{video_id}",
                }
            short_results = render_and_upload_shorts(
                bundle,
                parent_topic=topic,
                count=shorts_count,
                trend_report=result.get("trend_report"),
                rendered=rendered,
                cost_usd=result.get("visual_spend") or 0.0,
                long_video=long_video,
            )
            shorts_summary = summarize_shorts(short_results)
        except Exception as exc:  # noqa: BLE001 — Shorts must not kill the day
            logger.error(f"Shorts stage failed unexpectedly: {exc}")
            shorts_summary = {"overall": "failed", "uploaded": 0, "failed": shorts_count, "skipped": 0, "video_ids": [], "errors": [str(exc)]}
        if state is not None:
            state.short_uploads = short_results
            state.shorts_status = shorts_summary["overall"]
        logger.info(
            f"Shorts stage: {shorts_summary['uploaded']}/{shorts_count} uploaded "
            f"({shorts_summary['overall']}); ids: {shorts_summary['video_ids'] or 'none'}"
        )
    elif shorts_count > 0:
        logger.warning("No research bundle exists, so no Shorts can be derived; skipping Shorts.")
        shorts_summary = {"overall": "skipped", "uploaded": 0, "failed": 0, "skipped": shorts_count, "video_ids": [], "errors": ["no research bundle"]}

    # --- Cross-linking (internal linking, 26 Nov 2026): the long
    # video's description could not link the Shorts at upload time
    # (their IDs did not exist yet). Now that Shorts have uploaded,
    # update the long video's description with the Shorts links +
    # related videos + Sources. Best-effort: a failure here never
    # fails the day (the videos are already uploaded).
    cross_linked = False
    if long_ok and bundle is not None and script is not None:
        try:
            cross_linked, _new_description = cross_link_daily_uploads(
                long_yt_video_id=video_id,
                base_description=script.seo.description,
                shorts=short_results,
                bundle=bundle,
            )
            logger.info(
                f"Cross-linking for long video {video_id}: "
                f"{'updated' if cross_linked else 'NOT updated — see warning above'}"
            )
        except Exception as exc:  # noqa: BLE001 — cross-linking must never fail the day
            logger.warning(f"Cross-linking step failed unexpectedly: {exc}")

    # --- Best-effort telemetry (never fails the run).
    try:
        record_pipeline_run({
            "kind": "daily_run",
            "topic": topic,
            "pillar": pillar,
            "long_status": status,
            "long_video_id": video_id,
            "shorts_overall": shorts_summary["overall"],
            "shorts_uploaded": shorts_summary["uploaded"],
            "shorts_video_ids": shorts_summary["video_ids"],
            "cross_linked": cross_linked,
        })
    except Exception as exc:  # noqa: BLE001 — Sheet unavailable: log, continue
        logger.warning(f"Could not record daily pipeline run in Google Sheets: {exc}")

    # --- Notify (real values only).
    if long_ok:
        title = getattr(script.seo, "title", f"{topic} Breakdown") if script else topic
        duration = 0.0
        if script is not None:
            meta = getattr(script, "meta", None)
            duration = getattr(meta, "estimated_duration_sec", None) if meta is not None else None
            if duration is None:
                duration = getattr(script, "total_duration_sec", None)
            if duration is None:
                duration = 0.0
        notify_pipeline_success(
            video_id=video_id,
            title=title,
            topic=topic,
            pillar=pillar,
            duration_sec=duration,
            spend_usd=result.get("visual_spend") or 0.0,
            shorts_count=shorts_summary["uploaded"],
            video_path=result.get("rendered_video_path"),
            thumbnail_path=result.get("thumbnail_path"),
        )
        logger.info(
            f"✅ Daily run complete: long video {video_id} + "
            f"{shorts_summary['uploaded']}/{shorts_count} Shorts uploaded."
        )
        exit_code = 0
    else:
        error_msg = (
            f"Daily run: long video ended with status '{status}'. "
            f"Shorts: {shorts_summary['uploaded']}/{shorts_count} uploaded "
            f"({shorts_summary['overall']})."
        )
        logger.warning(f"⚠️ {error_msg}")
        if _is_topic_failure(result.get("error")):
            # Unproduceable topic (safety refusal, off-niche, or
            # unresearchable): the runner marks it failed and moves
            # on to the next queued topic — no Telegram alert for a
            # bad topic (mirrors scripts/dev_run.py).
            logger.warning(
                "Long video failed for a topic-attributable reason "
                f"({result.get('error')}); skipping failure notify."
            )
        else:
            notify_pipeline_failure(
                stage="daily_run",
                error_message=error_msg,
                topic=topic,
            )
        exit_code = 1

    return {
        "topic": topic,
        "pillar": pillar,
        "long_status": status,
        "long_video_id": video_id,
        "shorts": short_results,
        "shorts_summary": shorts_summary,
        # The failure reason, when the long video did not complete
        # (used by main() to decide whether the failure is
        # topic-attributable — mark failed, try the next topic —
        # or a real run failure to stop on).
        "error": result.get("error"),
        "exit_code": exit_code,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="TubePilot AI Daily Runner: 1 long video + N Shorts")
    parser.add_argument("--topic", type=str, default=None, help="Video topic to research and generate")
    parser.add_argument(
        "--pillar",
        type=str,
        default="ai_everything",
        choices=["ai_everything", "finance_to_technical", "tech_to_financial"],
        help="Content pillar (default: ai_everything)",
    )
    parser.add_argument(
        "--shorts-count",
        type=int,
        default=SHORTS_PER_DAY,
        choices=[0, 1, 2, 3],
        help="Shorts to produce from the day's topic (default: 3; 0 = long video only)",
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
    parser.add_argument(
        "--no-trending",
        action="store_true",
        help="Skip the daily trending-topic preference; always dequeue from the queue",
    )
    args = parser.parse_args()

    exit_code = 0
    try:
        if args.topic:
            # Explicit topic: a single attempt, no fall-through to
            # the queue (mirrors scripts/dev_run.py).
            summary = run_daily(topic=args.topic, pillar=args.pillar, shorts_count=args.shorts_count)
            return summary["exit_code"]
        trending = _prefer_trending_topic(args)
        # Queue path: dequeue in a loop until one topic succeeds,
        # the queue empties, or the per-run cap is hit. A topic
        # that fails for a topic-attributable reason is marked
        # failed in the Sheet and skipped; an empty queue exits 0
        # ("queue empty, nothing to do") — never a default topic,
        # never a crash, with or without --scheduled.
        for _attempt in range(_MAX_TOPICS_PER_RUN):
            resolved = _resolve_queued_topic(args, trending=trending)
            if resolved is None:
                # Scheduled run, empty queue: nothing to do.
                return 0
            topic, pillar, topic_id = resolved
            summary = run_daily(topic=topic, pillar=pillar, shorts_count=args.shorts_count)
            exit_code = summary["exit_code"]
            if exit_code == 0:
                return 0
            if _is_topic_failure(summary.get("error")):
                _mark_topic_failed(topic_id)
                continue
            return exit_code
        logger.error(
            f"No topic produced a video after {_MAX_TOPICS_PER_RUN} queued "
            "topics; failure alerts were sent for the non-topic failures."
        )
        return 1
    except SystemExit as e:
        exit_code = int(e.code or 0)
    except Exception as e:
        logger.error(f"❌ Fatal error in daily_run: {e}")
        notify_pipeline_failure(
            stage="daily_run",
            error_message=str(e),
            topic=args.topic,
        )
        exit_code = 2
    finally:
        if _is_in_codespace() and not args.no_stop:
            _stop_current_codespace()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
