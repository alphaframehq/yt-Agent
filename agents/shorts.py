"""Daily Shorts orchestration for TubePilot AI (1 long video + 3 Shorts/day).

Design choice (26 Nov 2026): the 3 Shorts derive from the SAME day's
topic/research as the long video — one research pass, one coherent
channel day, each Short teasing one angle of the long video, with
today's trend angles steering the hooks. Distinct short topics would
need 3 extra research + script cycles and 3x the LLM/time cost
inside the 60-minute daily job.

Isolation contract — the reason this module exists separately from
``orchestration/graph.py`` ``node_finalize``:

- Each Short is rendered/uploaded inside its own try/except. A
  Short's failure is recorded (status ``failed`` + error) and the
  loop continues with the next Short.
- Shorts failures NEVER change the long video's outcome, and a long
  video failure NEVER prevents the Shorts attempt: the daily runner
  (``scripts/daily_run.py``) runs Shorts from the research bundle
  whenever one exists, even when the long video failed to assemble
  or upload.
- Uploads go through the separate Shorts path
  (``agents/uploader.run_upload_short`` -> ``tools.youtube_api.
  upload_short`` -> ``#Shorts`` SEO, 9:16, <= 60s) and are tracked
  in the separate ``yt_shorts`` Sheet tab — never ``yt_videos``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from agents.uploader import run_upload_short
from models.research import ResearchBundle
from models.script import ShortsScript
from tools.trends import TrendReport

__all__ = [
    "SHORTS_PER_DAY",
    "upload_shorts",
    "render_and_upload_shorts",
]

SHORTS_PER_DAY = 3


def _short_result(
    index: int,
    title: str,
    status: str,
    yt_video_id: Optional[str] = None,
    error: Optional[str] = None,
    sheets_saved: bool = True,
) -> Dict[str, Any]:
    return {
        "index": index,
        "title": title,
        "status": status,  # uploaded | failed | skipped
        "yt_video_id": yt_video_id,
        "yt_url": f"https://youtu.be/{yt_video_id}" if yt_video_id else None,
        "error": error,
        "sheets_saved": sheets_saved,
    }


def upload_shorts(
    shorts: Sequence[Tuple[str, ShortsScript]],
    parent_topic: str,
    cost_usd: float = 0.0,
    bundle: Optional[ResearchBundle] = None,
    long_video: Optional[Any] = None,
    related: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Upload already-rendered Shorts, one by one, independently.

    ``shorts`` is a sequence of ``(video_path, ShortsScript)`` pairs.
    Each Short is uploaded inside its own try/except: a failure is
    recorded for that Short only and the remaining Shorts still run.
    Missing render files are recorded as ``skipped`` (not failed —
    nothing was attempted). Returns one result dict per Short.

    ``bundle`` (optional ResearchBundle) adds the "Sources /
    References" block to each Short's description;
    ``long_video`` (optional ``{"title", "yt_video_id", ...}`` or a
    bare YouTube video ID) adds the "Watch next" link to the same
    day's long video; ``related`` (optional previous channel videos)
    is appended to that block — when None and a bundle is given it
    is read best-effort from the yt_videos Sheet tab per Short.
    Both are best-effort: a linking failure never fails a Short's
    upload.
    """
    results: List[Dict[str, Any]] = []
    for index, (video_path, short_script) in enumerate(shorts):
        title = short_script.seo.title
        if not video_path or not Path(video_path).exists():
            logger.warning(f"Short {index} '{title}': render file missing, skipping upload.")
            results.append(_short_result(index, title, "skipped", error="render file missing"))
            continue
        try:
            result = run_upload_short(
                video_path=video_path,
                thumb_path=None,
                short_script=short_script,
                parent_topic=parent_topic,
                short_index=index,
                cost_usd=cost_usd,
                bundle=bundle,
                long_video=long_video,
                related=related,
            )
            logger.info(f"Short {index} uploaded: {result.yt_video_id} ('{title}')")
            results.append(
                _short_result(
                    index, title, "uploaded",
                    yt_video_id=result.yt_video_id,
                    sheets_saved=result.sheets_saved,
                )
            )
        except Exception as exc:  # noqa: BLE001 — per-Short isolation
            logger.error(f"Short {index} '{title}' failed to upload: {exc}")
            results.append(_short_result(index, title, "failed", error=str(exc)))
    return results


def render_and_upload_shorts(
    bundle: ResearchBundle,
    parent_topic: str,
    count: int = SHORTS_PER_DAY,
    trend_report: Optional[TrendReport] = None,
    rendered: Optional[Sequence[Tuple[str, ShortsScript]]] = None,
    cost_usd: float = 0.0,
    long_video: Optional[Any] = None,
    related: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Render (if needed) and upload ``count`` Shorts for the day.

    ``long_video`` (optional ``{"title", "yt_video_id", ...}`` or a
    bare YouTube video ID) is the same day's long video: each Short's
    description links it via a "Watch next" block, and the bundle
    adds the "Sources / References" block. ``related`` (optional
    previous channel videos) is appended to that "Watch next" block;
    when None it is read best-effort from the yt_videos Sheet tab
    per Short. Both are best-effort.

    If ``rendered`` pairs are supplied (the pipeline's
    ``node_finalize`` already rendered them), those files + scripts
    are uploaded as-is. Otherwise fresh Shorts are generated from
    the bundle (trend-steered) and assembled via
    ``agents.assemble.assemble_shorts`` (vertical 1080x1920), then
    uploaded. Generation/assembly of one Short failing does not stop
    the others: each Short is attempted independently and its outcome
    recorded.
    """
    if count <= 0:
        return []

    pairs: List[Tuple[str, ShortsScript]] = []
    if rendered:
        pairs = [(p, s) for p, s in rendered[:count]]
    else:
        from agents.assemble import assemble_shorts
        from agents.script import ScriptAgent

        try:
            shorts_list = ScriptAgent().generate_shorts(
                bundle, count=count, trend_report=trend_report
            )
        except Exception as exc:  # noqa: BLE001 — Shorts must not kill the day
            logger.error(f"Shorts generation failed, no Shorts will upload: {exc}")
            return [
                _short_result(i, "", "failed", error=f"shorts generation failed: {exc}")
                for i in range(count)
            ]
        for short_script in shorts_list:
            try:
                path = assemble_shorts(short_script, bundle_data=bundle)
                pairs.append((path, short_script))
            except Exception as exc:  # noqa: BLE001 — per-Short isolation
                logger.error(
                    f"Short '{short_script.seo.title}' failed to assemble: {exc}"
                )
                pairs.append(("", short_script))

    return upload_shorts(
        pairs, parent_topic=parent_topic, cost_usd=cost_usd,
        bundle=bundle, long_video=long_video, related=related,
    )


def summarize_shorts(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Small summary for logs / Telegram notify / pipeline-run record."""
    uploaded = [r for r in results if r["status"] == "uploaded"]
    failed = [r for r in results if r["status"] == "failed"]
    skipped = [r for r in results if r["status"] == "skipped"]
    if not results:
        overall = "skipped"
    elif uploaded and not failed and not skipped:
        overall = "complete"
    elif uploaded:
        overall = "partial"
    else:
        overall = "failed"
    return {
        "overall": overall,
        "uploaded": len(uploaded),
        "failed": len(failed),
        "skipped": len(skipped),
        "video_ids": [r["yt_video_id"] for r in uploaded if r.get("yt_video_id")],
        "errors": [r["error"] for r in failed if r.get("error")],
    }
