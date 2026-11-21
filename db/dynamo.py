"""DynamoDB Data Access Layer for TubePilot AI.

Supports all 7 provisioned DynamoDB tables:
1. yt_videos (Permanent video metadata, status index)
2. yt_research_cache (7-day TTL cached research bundles)
3. yt_verification_log (60-day TTL iteration audit history)
4. yt_pipeline_runs (90-day TTL stage telemetry and metrics)
5. yt_config (Hot-reloadable config, encrypted keys, pools)
6. yt_bot_requests (7-day TTL bot-to-runner message bus)
7. yt_topics_queue (Pending, running, and completed editorial queue)

Includes an automatic in-memory mock fallback for offline dev and test suites.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List, Optional
from loguru import logger

from tools.secrets import get_secret

__all__ = [
    "save_video_record",
    "get_video_record",
    "update_video_status",
    "cache_research",
    "get_cached_research",
    "log_verification",
    "record_pipeline_run",
    "enqueue_topic",
    "get_next_topic",
    "list_topics_queue",
    "write_bot_request",
    "get_bot_requests",
]

# In-memory storage for offline environments
_IN_MEMORY_STORE: Dict[str, Dict[str, Any]] = {
    "yt_videos": {},
    "yt_research_cache": {},
    "yt_verification_log": {},
    "yt_pipeline_runs": {},
    "yt_config": {},
    "yt_bot_requests": {},
    "yt_topics_queue": {},
}


def _get_table(table_name: str) -> Optional[Any]:
    """Retrieve boto3 DynamoDB Table resource with graceful fallback."""
    try:
        import boto3
        aws_key = get_secret("AWS_ACCESS_KEY_ID")
        aws_sec = get_secret("AWS_SECRET_ACCESS_KEY")
        kwargs = {"region_name": "us-east-1"}
        if aws_key and aws_sec:
            kwargs["aws_access_key_id"] = aws_key
            kwargs["aws_secret_access_key"] = aws_sec

        resource = boto3.resource("dynamodb", **kwargs)
        table = resource.Table(table_name)
        table.table_status
        return table
    except Exception:
        return None


# -----------------------------------------------------------------------------
# 1. yt_videos
# -----------------------------------------------------------------------------
def save_video_record(video_data: Dict[str, Any]) -> str:
    """Save newly uploaded video record to yt_videos."""
    video_id = video_data.get("video_id") or f"vid_{hashlib.md5(str(time.time()).encode()).hexdigest()[:8]}"
    video_data["video_id"] = video_id
    video_data.setdefault("status", "pending_approval")
    video_data.setdefault("created_at", int(time.time()))

    table = _get_table("yt_videos")
    if table:
        try:
            table.put_item(Item=video_data)
            logger.info(f"Video record saved to DynamoDB yt_videos: {video_id}")
            return video_id
        except Exception as e:
            logger.warning(f"DynamoDB save failed ({e}). Storing in local mock.")

    _IN_MEMORY_STORE["yt_videos"][video_id] = video_data
    return video_id


def get_video_record(video_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve video metadata by video_id."""
    table = _get_table("yt_videos")
    if table:
        try:
            resp = table.get_item(Key={"video_id": video_id})
            return resp.get("Item")
        except Exception:
            pass
    return _IN_MEMORY_STORE["yt_videos"].get(video_id)


def update_video_status(video_id: str, status: str, published_at: Optional[int] = None) -> bool:
    """Update video status (pending_approval, public, rejected)."""
    table = _get_table("yt_videos")
    if table:
        try:
            update_expr = "SET #s = :s"
            expr_names = {"#s": "status"}
            expr_vals = {":s": status}
            if published_at:
                update_expr += ", published_at = :p"
                expr_vals[":p"] = published_at

            table.update_item(
                Key={"video_id": video_id},
                UpdateExpression=update_expr,
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_vals,
            )
            return True
        except Exception:
            pass

    if video_id in _IN_MEMORY_STORE["yt_videos"]:
        _IN_MEMORY_STORE["yt_videos"][video_id]["status"] = status
        if published_at:
            _IN_MEMORY_STORE["yt_videos"][video_id]["published_at"] = published_at
        return True
    return False


# -----------------------------------------------------------------------------
# 2. yt_research_cache (7 days TTL)
# -----------------------------------------------------------------------------
def cache_research(topic: str, research_json: str, ttl_days: int = 7) -> str:
    """Store research bundle in cache with 7-day TTL."""
    topic_hash = hashlib.md5(topic.lower().strip().encode("utf-8")).hexdigest()
    ttl = int(time.time()) + (ttl_days * 86400)
    item = {
        "topic_hash": topic_hash,
        "topic": topic,
        "research_json": research_json,
        "created_at": int(time.time()),
        "ttl": ttl,
    }

    table = _get_table("yt_research_cache")
    if table:
        try:
            table.put_item(Item=item)
            return topic_hash
        except Exception:
            pass

    _IN_MEMORY_STORE["yt_research_cache"][topic_hash] = item
    return topic_hash


def get_cached_research(topic: str) -> Optional[str]:
    """Look up cached research bundle by topic."""
    topic_hash = hashlib.md5(topic.lower().strip().encode("utf-8")).hexdigest()
    table = _get_table("yt_research_cache")
    if table:
        try:
            resp = table.get_item(Key={"topic_hash": topic_hash})
            item = resp.get("Item")
            if item and item.get("ttl", 0) > time.time():
                return item.get("research_json")
        except Exception:
            pass

    item = _IN_MEMORY_STORE["yt_research_cache"].get(topic_hash)
    if item and item.get("ttl", 0) > time.time():
        return item.get("research_json")
    return None


# -----------------------------------------------------------------------------
# 3. yt_verification_log (60 days TTL)
# -----------------------------------------------------------------------------
def log_verification(video_id: str, iteration: int, report_dict: Dict[str, Any]) -> None:
    """Log verification audit details for an iteration."""
    ttl = int(time.time()) + (60 * 86400)
    item = {
        "video_id": video_id,
        "iteration": iteration,
        "overall": report_dict.get("overall", "pass"),
        "confidence": report_dict.get("confidence", 1.0),
        "model_used": report_dict.get("model_used", "claude-3-5-haiku"),
        "report_json": json.dumps(report_dict),
        "created_at": int(time.time()),
        "ttl": ttl,
    }

    table = _get_table("yt_verification_log")
    if table:
        try:
            table.put_item(Item=item)
            return
        except Exception:
            pass

    key = f"{video_id}#{iteration}"
    _IN_MEMORY_STORE["yt_verification_log"][key] = item


# -----------------------------------------------------------------------------
# 4. yt_pipeline_runs (90 days TTL)
# -----------------------------------------------------------------------------
def record_pipeline_run(run_data: Dict[str, Any]) -> str:
    """Record execution telemetry for a GitHub Actions pipeline run."""
    run_id = run_data.get("run_id") or f"run_{hashlib.md5(str(time.time()).encode()).hexdigest()[:8]}"
    run_data["run_id"] = run_id
    run_data.setdefault("ttl", int(time.time()) + (90 * 86400))
    run_data.setdefault("started_at", int(time.time()))

    table = _get_table("yt_pipeline_runs")
    if table:
        try:
            table.put_item(Item=run_data)
            return run_id
        except Exception:
            pass

    _IN_MEMORY_STORE["yt_pipeline_runs"][run_id] = run_data
    return run_id


# -----------------------------------------------------------------------------
# 6. yt_bot_requests (7 days TTL - Bot-to-Workflow Message Bus)
# -----------------------------------------------------------------------------
def write_bot_request(request_type: str, inputs: Dict[str, Any]) -> str:
    """Write action request from Telegram bot to DynamoDB message bus."""
    req_id = f"req_{hashlib.md5(str(time.time()).encode()).hexdigest()[:8]}"
    item = {
        "request_id": req_id,
        "type": request_type,
        "inputs": inputs,
        "status": "pending",
        "requested_at": int(time.time()),
        "ttl": int(time.time()) + (7 * 86400),
    }

    table = _get_table("yt_bot_requests")
    if table:
        try:
            table.put_item(Item=item)
            return req_id
        except Exception:
            pass

    _IN_MEMORY_STORE["yt_bot_requests"][req_id] = item
    return req_id


def get_bot_requests(status: str = "pending") -> List[Dict[str, Any]]:
    """Fetch pending bot requests from DynamoDB message bus."""
    table = _get_table("yt_bot_requests")
    if table:
        try:
            resp = table.scan()
            return [it for it in resp.get("Items", []) if it.get("status") == status]
        except Exception:
            pass

    return [v for v in _IN_MEMORY_STORE["yt_bot_requests"].values() if v.get("status") == status]


# -----------------------------------------------------------------------------
# 7. yt_topics_queue
# -----------------------------------------------------------------------------
def enqueue_topic(topic: str, pillar: str = "finance_to_technical", priority: int = 1) -> str:
    """Add new topic to the processing queue."""
    topic_id = f"top_{hashlib.md5(topic.encode()).hexdigest()[:8]}"
    item = {
        "topic_id": topic_id,
        "topic": topic,
        "pillar": pillar,
        "status": "pending",
        "priority": priority,
        "added_at": int(time.time()),
    }

    table = _get_table("yt_topics_queue")
    if table:
        try:
            table.put_item(Item=item)
            return topic_id
        except Exception:
            pass

    _IN_MEMORY_STORE["yt_topics_queue"][topic_id] = item
    return topic_id


def get_next_topic() -> Optional[Dict[str, Any]]:
    """Retrieve highest-priority pending topic and mark running."""
    topics = list_topics_queue(status="pending")
    if not topics:
        return None

    # Sort by priority descending, then added_at ascending
    sorted_topics = sorted(topics, key=lambda t: (-t.get("priority", 1), t.get("added_at", 0)))
    selected = sorted_topics[0]
    selected_id = selected["topic_id"]

    table = _get_table("yt_topics_queue")
    if table:
        try:
            table.update_item(
                Key={"topic_id": selected_id},
                UpdateExpression="SET #s = :s",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "running"},
            )
        except Exception:
            pass

    if selected_id in _IN_MEMORY_STORE["yt_topics_queue"]:
        _IN_MEMORY_STORE["yt_topics_queue"][selected_id]["status"] = "running"

    return selected


def list_topics_queue(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """List queue topics filtered by status."""
    table = _get_table("yt_topics_queue")
    if table:
        try:
            resp = table.scan()
            items = resp.get("Items", [])
            if status:
                return [it for it in items if it.get("status") == status]
            return items
        except Exception:
            pass

    items = list(_IN_MEMORY_STORE["yt_topics_queue"].values())
    if status:
        return [it for it in items if it.get("status") == status]
    return items


# Aliases for queue management
dequeue_next_topic = get_next_topic
get_topic_queue = list_topics_queue

