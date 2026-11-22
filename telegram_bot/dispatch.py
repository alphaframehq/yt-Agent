"""GitHub Actions Workflow Dispatcher and Message Bus Bridge for TubePilot AI.

Dispatches pipeline execution without webhooks or public server URLs:
1. Writes dispatch event to DynamoDB yt_bot_requests
2. Enqueues topic in DynamoDB yt_topics_queue
3. Calls GitHub Actions REST API: POST /repos/{owner}/{repo}/actions/workflows/daily.yml/dispatches
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional
import httpx
from loguru import logger

from db.dynamo import enqueue_topic, write_bot_request
from tools.secrets import get_secret

__all__ = ["dispatch_pipeline_run"]


def dispatch_pipeline_run(topic: str, pillar: str = "finance_to_technical") -> Dict[str, Any]:
    """Trigger GitHub Actions run for topic using GH_PAT and DynamoDB message bus."""
    logger.info(f"Bot triggering workflow dispatch for topic: '{topic}' [{pillar}]")

    # 1. Enqueue topic in DynamoDB
    top_id = enqueue_topic(topic=topic, pillar=pillar, priority=10)

    # 2. Write request to DynamoDB message bus
    req_id = write_bot_request(
        request_type="run_pipeline",
        inputs={"topic": topic, "pillar": pillar, "topic_id": top_id},
    )

    # 3. Call GitHub Actions API
    gh_pat = get_secret("GH_PAT") or os.getenv("GH_PAT") or os.getenv("GITHUB_TOKEN")
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
        details = "GH_PAT not configured. Request recorded in DynamoDB topics queue for scheduled cron run."

    return {
        "dispatched": dispatched,
        "topic_id": top_id,
        "request_id": req_id,
        "details": details,
        "topic": topic,
    }
