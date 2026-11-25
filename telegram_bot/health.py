# LOCAL PRODUCTION PATCH — 2026-11-25 — alphaframehq/yt-Agent main c39f42541b8e784dcf41c09583dc57227be07667
# P0 fix (#22): get_system_ping() counts real pending approvals from yt_videos, pings the GitHub API endpoint,
#   sums cost_usd actually recorded on video records, and queries the last GitHub Actions run — every value that
#   cannot be verified is labelled 'unknown/not checked'. Removed hardcoded pending_approvals=1, mtd_cost=0.38,
#   'GitHub Actions: Last run succeeded'. get_cost_summary() no longer fabricates $0.08-$0.14 / $0.38.
#   get_pipeline_logs() returns 'no logs found for <run_id>' — no constant Stripe log lines.
# Local patch only — do NOT push to GitHub.
"""Health check, spend tracking, and diagnostics for TubePilot AI Telegram bot.

Implements:
- /ping -> Full system health diagnostic in < 5 seconds
- /cost [video_id] -> Financial expenditure audit
- /logs [run_id] -> Pipeline log telemetry
"""

from __future__ import annotations

import time
from typing import Any, Dict, List
from loguru import logger

from config_manager import get_config
from db.sheets import get_video_record, list_topics_queue
from tools.secrets import _get_secret_optional

__all__ = [
    "get_system_ping",
    "get_cost_summary",
    "get_pipeline_logs",
]


def get_system_ping() -> Dict[str, Any]:
    """Execute rapid <5s health probe across all infrastructure components.

    P0 #22: every figure is either read from a real source or labelled
    "unknown/not checked". Nothing is hardcoded: pending approvals are
    counted from yt_videos, spend is summed from cost_usd actually stored
    on video records, and the last GitHub Actions run is queried live.
    """
    t_start = time.perf_counter()

    # 1. Config status
    is_paused = bool(get_config("paused"))
    active_verify = get_config("active_models.verify") or "bedrock/claude-3-5-haiku"

    # 2. Topic queue depth (real Google Sheets read via list_topics_queue)
    try:
        topics_pending = len(list_topics_queue(status="pending"))
        topics_running = len(list_topics_queue(status="running"))
        queue_error = ""
    except Exception as e:
        logger.error(f"/ping: could not read topic queue: {e}")
        topics_pending = topics_running = None
        queue_error = str(e)

    # 3. Pending approvals — real count from yt_videos, never a baseline.
    try:
        from db.sheets import list_recent_videos as _list_recent_videos
        _recent = _list_recent_videos(limit=100)
        pending_approvals = sum(1 for v in _recent if v.get("status") == "pending_approval")
        recent_for_spend = _recent
    except Exception as e:
        logger.error(f"/ping: could not read video records: {e}")
        pending_approvals = None
        recent_for_spend = []

    # 4. Google Sheets — no live ping is made here, so do not claim "Connected".
    sheets_status = "unknown/not checked (no live Google Sheets ping made; queue/video reads above are the only Google Sheets access attempted)"

    # 5. LLM provider keys — key presence only; never "Ready" without a call.
    gemini_key = _get_secret_optional("GEMINI_KEY")
    gemini_status = "key set (not pinged)" if gemini_key else "⚠️ Unset"
    openrouter_key = _get_secret_optional("OPENROUTER_KEY")
    openrouter_status = "key set (not pinged)" if openrouter_key else "⚠️ Unset"
    bedrock_status = "key set (not pinged)" if _get_secret_optional("AWS_ACCESS_KEY_ID") else "⚠️ Unset"

    # 6. Spend — sum of cost_usd actually stored on recent video records.
    #    This is NOT a verified month-to-date total (no spend ledger exists).
    recorded_costs = [v.get("cost_usd") for v in recent_for_spend if isinstance(v.get("cost_usd"), (int, float))]
    recorded_spend = round(sum(recorded_costs), 3) if recorded_costs else None

    # 7. GitHub API endpoint ping + last workflow run (real query, best effort).
    gh_status = "unknown/not checked (GH_PAT not configured)"
    gh_last_run = "unknown/not checked"
    try:
        import httpx
        import os
        try:
            resp = httpx.get("https://api.github.com", timeout=5.0)
            gh_status = f"reachable (HTTP {resp.status_code})" if resp.status_code == 200 else f"HTTP {resp.status_code}"
        except Exception as e:
            gh_status = f"unreachable: {e}"
        gh_pat = _get_secret_optional("GH_PAT") or os.getenv("GITHUB_TOKEN")
        full_repo = os.getenv("GITHUB_REPOSITORY", "")
        if gh_pat and "/" in full_repo:
            owner, repo = full_repo.split("/", 1)
            with httpx.Client(timeout=10.0) as client:
                r = client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/actions/runs?per_page=1",
                    headers={"Authorization": f"Bearer {gh_pat}", "Accept": "application/vnd.github.v3+json"},
                )
                if r.status_code == 200:
                    runs = r.json().get("workflow_runs", [])
                    if runs:
                        run = runs[0]
                        gh_last_run = (
                            f"{run.get('name', 'run')} — status: {run.get('status')}, "
                            f"conclusion: {run.get('conclusion')}, created: {run.get('created_at')}"
                        )
                    else:
                        gh_last_run = "no workflow runs found"
                else:
                    gh_last_run = f"unknown/not checked (GitHub API HTTP {r.status_code})"
        elif gh_pat:
            gh_last_run = "unknown/not checked (GITHUB_REPOSITORY not set)"
    except Exception as e:
        logger.error(f"/ping: GitHub status check failed: {e}")
        gh_last_run = f"unknown/not checked ({e})"

    elapsed = round(time.perf_counter() - t_start, 2)

    import os
    run_id = os.getenv("GITHUB_RUN_ID")
    host_env = f"GitHub Actions Cloud (Runner #{run_id})" if os.getenv("GITHUB_ACTIONS") == "true" else "Local / Codespace"

    def _u(value, suffix=""):
        return f"`{value}{suffix}`" if value is not None else "unknown/not checked"

    report_text = (
        f"⚡️ *TUBEPILOT AI // SYSTEM HEALTH REPORT* ({elapsed}s)\n\n"
        f"• *Host Environment:* `{host_env}`\n"
        f"• *Scheduler Status:* {'⏸ PAUSED' if is_paused else '▶️ ACTIVE'}\n"
        f"• *Active Verify Model:* `{active_verify}`\n"
        f"• *Google Sheets:* {sheets_status}\n"
        f"• *Queue Depth:* {_u(topics_pending)} pending, {_u(topics_running)} running\n"
        f"• *Pending Approvals:* {_u(pending_approvals)} video(s)\n"
        f"• *Recorded Spend (sum of cost_usd on last {len(recent_for_spend)} video records):* "
        f"{f'${recorded_spend:.3f} USD' if recorded_spend is not None else 'unknown/not checked (no cost_usd recorded)'}\n"
        f"• *Month-to-Date Spend:* unknown/not checked (no spend ledger queried)\n\n"
        f"🤖 *LLM Provider Status (key presence only — not pinged):*\n"
        f"• Gemini 2.0/2.5: {gemini_status}\n"
        f"• AWS Bedrock Claude: {bedrock_status}\n"
        f"• OpenRouter (:free): {openrouter_status}\n\n"
        f"🚀 *GitHub API:* {gh_status}\n"
        f"🚀 *GitHub Actions — Last Run:* {gh_last_run}"
    )

    return {
        "text": report_text,
        "elapsed_sec": elapsed,
        "is_paused": is_paused,
        "topics_pending": topics_pending,
        "pending_approvals": pending_approvals,
        "recorded_spend_usd": recorded_spend,
    }


def get_cost_summary(video_id: str = "") -> str:
    """Return spend report for specific video or global month-to-date.

    P0 #22: only figures actually stored on a record (or in config for the
    budget cap) are shown. No fabricated averages or month-to-date totals.
    """
    if video_id:
        rec = get_video_record(video_id)
        if rec:
            cost = rec.get("cost_usd")
            cost_line = f"• Total Cost: `${cost:.3f} USD`" if isinstance(cost, (int, float)) else "• Total Cost: unknown/not recorded (no cost_usd on this record)"
            return (
                f"💰 *Cost Audit for Video `{video_id}`:*\n"
                f"• Title: {rec.get('title', 'Unknown')}\n"
                f"{cost_line}\n"
                "• Audio / assembly / visuals breakdown: unknown/not checked (no per-stage cost recorded)"
            )
        return f"❌ Video record `{video_id}` not found."

    budget = get_config("budget_ai_per_video")
    budget_line = f"• Current Video Budget Cap: `${budget} USD`" if budget is not None else "• Current Video Budget Cap: unknown/not checked"
    try:
        from db.sheets import list_recent_videos as _list_recent_videos
        _recent = _list_recent_videos(limit=100)
        _costs = [v.get("cost_usd") for v in _recent if isinstance(v.get("cost_usd"), (int, float))]
        partial = f"${round(sum(_costs), 3):.3f} USD (sum of cost_usd on last {len(_recent)} video records)" if _costs else "unknown/not checked (no cost_usd recorded on recent video records)"
    except Exception as e:
        partial = f"unknown/not checked ({e})"

    return (
        "💰 *TubePilot AI Cost & Spend Tracker*\n\n"
        f"{budget_line}\n"
        f"• Recorded Spend (recent video records only): `{partial}`\n"
        "• Average Cost per Video: unknown/not checked (no verified cost ledger)\n"
        "• Month-to-date Spend: unknown/not checked (no month-to-date spend ledger is queried)\n"
        "• YouTube Storage Cost: unknown/not checked\n"
        "• Google Sheets Cost: unknown/not checked (no billing is tracked for the sheet)"
    )


def get_pipeline_logs(run_id: str = "") -> str:
    """Return recent telemetry logs from the pipeline.

    P0 #22: this bot queries no pipeline log store, so there are no log
    lines to show. Never return constant canned log lines for a run_id.
    """
    target = run_id or "latest"
    return (
        f"📋 *Pipeline Execution Logs (`{target}`):*\n\n"
        f"❌ No logs found for `{target}` — no pipeline log store is queried by this bot, "
        "so no log lines can be shown."
    )
