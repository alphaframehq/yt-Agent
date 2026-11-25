# LOCAL PRODUCTION PATCH — 2026-11-25 — alphaframehq/yt-Agent main c39f42541b8e784dcf41c09583dc57227be07667
# P0 fix (#20): dispatch_pipeline_run() returns dispatched=False with explicit details in every failure path
#   (no GH_PAT, non-204 GitHub response, HTTP error) — dispatched=True only on HTTP 204. No fake success.
# Local patch only — do NOT push to GitHub.
"""GitHub Actions Workflow Dispatcher and Message Bus Bridge for TubePilot AI.

Dispatches pipeline execution without webhooks or public server URLs:
1. Writes dispatch event to Google Sheets yt_bot_requests
2. Enqueues topic in Google Sheets yt_topics_queue
3. Calls GitHub Actions REST API: POST /repos/{owner}/{repo}/actions/workflows/daily.yml/dispatches
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional
import httpx
from loguru import logger

from db.sheets import enqueue_topic, write_bot_request
from tools.secrets import _get_secret_optional

__all__ = ["dispatch_pipeline_run"]


def dispatch_pipeline_run(topic: str, pillar: str = "finance_to_technical") -> Dict[str, Any]:
    """Trigger GitHub Actions run for topic using GH_PAT and Google Sheets message bus."""
    logger.info(f"Bot triggering workflow dispatch for topic: '{topic}' [{pillar}]")

    # 1. Enqueue topic in Google Sheets
    top_id = enqueue_topic(topic=topic, pillar=pillar, priority=10)

    # 2. Write request to Google Sheets message bus
    req_id = write_bot_request(
        request_type="run_pipeline",
        inputs={"topic": topic, "pillar": pillar, "topic_id": top_id},
    )

    # 3. Call GitHub Actions API
    gh_pat = _get_secret_optional("GH_PAT") or os.getenv("GITHUB_TOKEN")
    full_repo = os.getenv("GITHUB_REPOSITORY", "")
    if "/" in full_repo:
        repo_owner, repo_name = full_repo.split("/", 1)
    else:
        repo_owner = os.getenv("GH_REPO_OWNER", "alphaframehq")
        repo_name = os.getenv("GH_REPO_NAME", "yt-Agent")

    dispatched = False
    details = ""

    if gh_pat:
        url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/actions/workflows/daily.yml/dispatches"
        headers = {
            "Authorization": f"Bearer {gh_pat}",
            "Accept": "application/vnd.github.v3+json",
        }
        payload = {
            "ref": "main",
            "inputs": {
                "topic": topic,
                "pillar": pillar,
            },
        }

        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(url, headers=headers, json=payload)
                if resp.status_code == 204:
                    dispatched = True
                    details = "GitHub Actions runner dispatched successfully."
                else:
                    details = f"GitHub API responded with code {resp.status_code}: {resp.text}"
        except Exception as e:
            details = f"HTTP dispatch request failed: {e}"
    else:
        # dispatched stays False: no GitHub dispatch was even attempted.
        details = "❌ Dispatch failed: GH_PAT not configured — GitHub dispatch not attempted. Request recorded in Google Sheets topics queue for scheduled cron run."

    return {
        "dispatched": dispatched,
        "topic_id": top_id,
        "request_id": req_id,
        "details": details,
        "topic": topic,
    }
