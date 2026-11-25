"""Google Sheets Data Access Layer for TubePilot AI.

Replaces ``db/dynamo.py`` (25 Nov 2026 — DynamoDB removed entirely;
user decision: all non-secret data lives in Google Sheets, secrets stay
in GitHub repo secrets / env vars).

One Google Spreadsheet, one tab per former DynamoDB table, plus a tab
for YouTube insights (``YTInsights`` in ``models/research.py``):

1. yt_videos (video metadata, key: video_id)
2. yt_research_cache (7-day TTL cached research bundles, key: topic_hash)
3. yt_verification_log (60-day TTL iteration audit history,
   key: video_id + iteration)
4. yt_pipeline_runs (90-day TTL stage telemetry, key: run_id)
5. yt_config (hot-reloadable config rows, key: key)
6. yt_bot_requests (7-day TTL bot-to-runner message bus, key: request_id)
7. yt_topics_queue (editorial queue, key: topic_id)
8. yt_insights (YouTube landscape insights per topic, key: topic_hash)

Storage layout: the first row of every tab is the header (see
``TAB_HEADERS``). Values are stored as plain Sheets values, except the
columns listed in ``JSON_COLUMNS``, which hold a JSON-encoded string
(for dicts / lists). Empty cells read back as ``None``.

Configuration (env only, never in the sheet):
- ``GOOGLE_SHEETS_ID`` — the spreadsheet ID.
- ``GOOGLE_SERVICE_ACCOUNT_JSON`` — the service-account credential:
  either the raw JSON string or a path to a ``.json`` key file. The
  service account must be shared on the spreadsheet as Editor.

Production behaviour: any Sheets failure raises ``RuntimeError``.
There is NO silent in-memory fallback — a store that cannot be
reached must fail loudly (same rule PR #1 applied to DynamoDB).
Tests use ``tests/_sheets_fake.py`` (an in-memory fake Sheets
service), never this module's internals.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

from loguru import logger

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
    "list_recent_videos",
    "clear_topic_queue",
    "get_latest_verification_log",
    "save_yt_insights",
    "get_yt_insights",
    "dequeue_next_topic",
    "get_topic_queue",
    "TAB_HEADERS",
    "JSON_COLUMNS",
]

_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# Tab -> ordered column headers (first row of each tab). Single source
# of truth; db/setup_sheets.py creates tabs from this mapping.
TAB_HEADERS: Dict[str, List[str]] = {
    "yt_videos": [
        "video_id", "title", "topic", "yt_video_id", "yt_url",
        "status", "created_at", "published_at", "cost_usd", "extra_json",
    ],
    "yt_research_cache": [
        "topic_hash", "topic", "research_json", "created_at", "ttl",
    ],
    "yt_verification_log": [
        "video_id", "iteration", "overall", "confidence", "model_used",
        "report_json", "created_at", "ttl",
    ],
    "yt_pipeline_runs": ["run_id", "started_at", "ttl", "extra_json"],
    "yt_config": ["key", "value_json"],
    "yt_bot_requests": [
        "request_id", "type", "inputs_json", "status", "requested_at", "ttl",
    ],
    "yt_topics_queue": [
        "topic_id", "topic", "pillar", "status", "priority", "added_at",
    ],
    "yt_insights": [
        "topic_hash", "topic", "top_titles_json", "common_tags_json",
        "gaps_json", "created_at",
    ],
}

# Columns whose cells hold a JSON-encoded string (dicts / lists).
JSON_COLUMNS: Dict[str, set] = {
    "yt_videos": {"extra_json"},
    "yt_bot_requests": {"inputs_json"},
    "yt_config": {"value_json"},
    "yt_insights": {"top_titles_json", "common_tags_json", "gaps_json"},
}

# Tabs whose rows carry a `ttl` (epoch seconds) — expired rows are
# ignored on read (DynamoDB TTL parity: DynamoDB deleted them).
_TTL_TABS = {"yt_research_cache", "yt_verification_log", "yt_pipeline_runs", "yt_bot_requests"}


# ---------------------------------------------------------------------------
# Service construction (lazy, cached)
# ---------------------------------------------------------------------------

_service: Any = None


def _get_spreadsheet_id() -> str:
    sheet_id = os.environ.get("GOOGLE_SHEETS_ID", "").strip()
    if not sheet_id:
        raise RuntimeError(
            "GOOGLE_SHEETS_ID env var is required: set it to the ID of "
            "the Google Spreadsheet holding the yt_* tabs (see docs/SETUP.md §3)."
        )
    return sheet_id


def _get_service() -> Any:
    """Build (once) the Google Sheets API service from the service account.

    Raises:
        RuntimeError: If ``GOOGLE_SHEETS_ID`` /
            ``GOOGLE_SERVICE_ACCOUNT_JSON`` is missing or unusable, or
            the Sheets client cannot be built.
    """
    global _service
    if _service is not None:
        return _service

    sheet_id = _get_spreadsheet_id()  # validates presence first
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON env var is required: set it to "
            "the service-account JSON (raw JSON string or path to a .json "
            "key file) for the account shared on spreadsheet "
            f"'{sheet_id}' as Editor (see docs/SETUP.md §3)."
        )
    try:
        if raw.startswith("{"):
            info = json.loads(raw)
        else:
            with open(raw, "r", encoding="utf-8") as fh:
                info = json.load(fh)
    except Exception as exc:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON (nor a readable "
            f".json file path): {exc}"
        ) from exc

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_info(
            info, scopes=[_SHEETS_SCOPE]
        )
        _service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        logger.info("Google Sheets connected (spreadsheet '{}')", sheet_id)
        return _service
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Could not build Google Sheets client: {exc}") from exc


# ---------------------------------------------------------------------------
# Row encoding / decoding
# ---------------------------------------------------------------------------


def _to_cell(column: str, tab: str, value: Any) -> Any:
    """Encode one value for a Sheets cell."""
    if value is None:
        return ""
    if column in JSON_COLUMNS.get(tab, set()):
        return json.dumps(value)
    return value


def _from_cell(column: str, tab: str, value: Any) -> Any:
    """Decode one Sheets cell value."""
    if value == "" or value is None:
        return None
    if column in JSON_COLUMNS.get(tab, set()) and isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            # A hand-edited cell that is not valid JSON — return it raw
            # rather than crashing the read.
            return value
    return value


def _row_to_values(tab: str, row: Dict[str, Any]) -> List[Any]:
    headers = TAB_HEADERS[tab]
    return [_to_cell(col, tab, row.get(col)) for col in headers]


def _values_to_row(tab: str, values: List[Any]) -> Dict[str, Any]:
    headers = TAB_HEADERS[tab]
    padded = list(values) + [""] * (len(headers) - len(values))
    return {
        col: _from_cell(col, tab, padded[i]) for i, col in enumerate(headers)
    }


# ---------------------------------------------------------------------------
# Low-level tab I/O (all failures raise RuntimeError)
# ---------------------------------------------------------------------------


def _read_rows(tab: str) -> List[Dict[str, Any]]:
    """Read all data rows of *tab* (header row skipped)."""
    try:
        resp = (
            _get_service()
            .spreadsheets()
            .values()
            .get(spreadsheetId=_get_spreadsheet_id(), range=f"{tab}!A:Z")
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Google Sheets read failed for tab '{tab}': {exc}") from exc
    values = resp.get("values", [])
    if not values:
        return []
    # values[0] is the header row; ignore it (setup guarantees the schema).
    return [_values_to_row(tab, row) for row in values[1:] if any(c != "" for c in row)]


def _read_rows_indexed(tab: str) -> List[tuple]:
    """Read data rows as (sheet_row_number, row_dict) pairs (row 1 = header)."""
    try:
        resp = (
            _get_service()
            .spreadsheets()
            .values()
            .get(spreadsheetId=_get_spreadsheet_id(), range=f"{tab}!A:Z")
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Google Sheets read failed for tab '{tab}': {exc}") from exc
    values = resp.get("values", [])
    out = []
    for i, row in enumerate(values[1:], start=2):
        if any(c != "" for c in row):
            out.append((i, _values_to_row(tab, row)))
    return out


def _append_row(tab: str, row: Dict[str, Any]) -> None:
    try:
        _get_service().spreadsheets().values().append(
            spreadsheetId=_get_spreadsheet_id(),
            range=f"{tab}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [_row_to_values(tab, row)]},
        ).execute()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Google Sheets append failed for tab '{tab}': {exc}") from exc


def _update_row(tab: str, row_number: int, row: Dict[str, Any]) -> None:
    try:
        _get_service().spreadsheets().values().update(
            spreadsheetId=_get_spreadsheet_id(),
            range=f"{tab}!A{row_number}:Z{row_number}",
            valueInputOption="USER_ENTERED",
            body={"values": [_row_to_values(tab, row)]},
        ).execute()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Google Sheets update failed for tab '{tab}' row {row_number}: {exc}"
        ) from exc


def _rewrite_tab(tab: str, rows: List[Dict[str, Any]]) -> None:
    """Replace all data rows of *tab* with *rows* (header row kept)."""
    service = _get_service()
    sheet_id = _get_spreadsheet_id()
    try:
        service.spreadsheets().values().clear(
            spreadsheetId=sheet_id, range=f"{tab}!A2:Z"
        ).execute()
        if rows:
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"{tab}!A2",
                valueInputOption="USER_ENTERED",
                body={"values": [_row_to_values(tab, r) for r in rows]},
            ).execute()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Google Sheets rewrite failed for tab '{tab}': {exc}") from exc


def _upsert_row(tab: str, key_col: str, row: Dict[str, Any]) -> None:
    """Insert *row* or replace the existing row with the same key value."""
    key_val = row.get(key_col)
    for row_number, existing in _read_rows_indexed(tab):
        if existing.get(key_col) == key_val:
            _update_row(tab, row_number, row)
            return
    _append_row(tab, row)


def _upsert_row_composite(tab: str, key_cols: List[str], row: Dict[str, Any]) -> None:
    """Insert *row* or replace the row matching all *key_cols* values."""
    for row_number, existing in _read_rows_indexed(tab):
        if all(existing.get(c) == row.get(c) for c in key_cols):
            _update_row(tab, row_number, row)
            return
    _append_row(tab, row)


def _is_expired(row: Dict[str, Any]) -> bool:
    ttl = row.get("ttl")
    return isinstance(ttl, (int, float)) and ttl <= time.time()


def _split_known_extra(
    data: Dict[str, Any], known: List[str]
) -> tuple:
    """Split *data* into (known-columns dict, extra dict)."""
    known_row = {k: data.get(k) for k in known}
    extra = {k: v for k, v in data.items() if k not in known}
    return known_row, extra


# -----------------------------------------------------------------------------
# 1. yt_videos
# -----------------------------------------------------------------------------
def save_video_record(video_data: Dict[str, Any]) -> str:
    """Save newly uploaded video record to the yt_videos tab (upsert)."""
    video_id = video_data.get("video_id") or f"vid_{hashlib.md5(str(time.time()).encode()).hexdigest()[:8]}"
    video_data["video_id"] = video_id
    video_data.setdefault("status", "pending_approval")
    video_data.setdefault("created_at", int(time.time()))

    known = [c for c in TAB_HEADERS["yt_videos"] if c != "extra_json"]
    row, extra = _split_known_extra(video_data, known)
    row["extra_json"] = extra
    _upsert_row("yt_videos", "video_id", row)
    logger.info("Video record saved to Google Sheets yt_videos: {}", video_id)
    return video_id


def get_video_record(video_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve video metadata by video_id."""
    for row in _read_rows("yt_videos"):
        if row.get("video_id") == video_id:
            rec = {k: v for k, v in row.items() if k != "extra_json"}
            rec.update(row.get("extra_json") or {})
            return rec
    return None


def update_video_status(video_id: str, status: str, published_at: Optional[int] = None) -> bool:
    """Update video status (pending_approval, public, rejected)."""
    for row_number, row in _read_rows_indexed("yt_videos"):
        if row.get("video_id") == video_id:
            row["status"] = status
            if published_at:
                row["published_at"] = published_at
            _update_row("yt_videos", row_number, row)
            return True
    return False


# -----------------------------------------------------------------------------
# 2. yt_research_cache (7 days TTL)
# -----------------------------------------------------------------------------
def cache_research(topic: str, research_json: str, ttl_days: int = 7) -> str:
    """Store research bundle in cache with 7-day TTL."""
    topic_hash = hashlib.md5(topic.lower().strip().encode("utf-8")).hexdigest()
    item = {
        "topic_hash": topic_hash,
        "topic": topic,
        "research_json": research_json,
        "created_at": int(time.time()),
        "ttl": int(time.time()) + (ttl_days * 86400),
    }
    _upsert_row("yt_research_cache", "topic_hash", item)
    return topic_hash


def get_cached_research(topic: str) -> Optional[str]:
    """Look up cached research bundle by topic (expired entries ignored)."""
    topic_hash = hashlib.md5(topic.lower().strip().encode("utf-8")).hexdigest()
    for row in _read_rows("yt_research_cache"):
        if row.get("topic_hash") == topic_hash and not _is_expired(row):
            return row.get("research_json")
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
        # Production fix (PR #1, 25 Nov 2026, preserved in the Sheets
        # migration): a missing overall/confidence must NOT default to
        # a passing verdict. "not_run" / 0.0 means no verification
        # actually ran; callers (and /script) must treat that as
        # unverified, never as PASS.
        "overall": report_dict.get("overall", "not_run"),
        "confidence": report_dict.get("confidence", 0.0),
        "model_used": report_dict.get("model_used", "unknown"),
        "report_json": json.dumps(report_dict),
        "created_at": int(time.time()),
        "ttl": ttl,
    }
    _upsert_row_composite("yt_verification_log", ["video_id", "iteration"], item)


# -----------------------------------------------------------------------------
# 4. yt_pipeline_runs (90 days TTL)
# -----------------------------------------------------------------------------
def record_pipeline_run(run_data: Dict[str, Any]) -> str:
    """Record execution telemetry for a GitHub Actions pipeline run."""
    run_id = run_data.get("run_id") or f"run_{hashlib.md5(str(time.time()).encode()).hexdigest()[:8]}"
    run_data["run_id"] = run_id
    run_data.setdefault("ttl", int(time.time()) + (90 * 86400))
    run_data.setdefault("started_at", int(time.time()))

    known = [c for c in TAB_HEADERS["yt_pipeline_runs"] if c != "extra_json"]
    row, extra = _split_known_extra(run_data, known)
    row["extra_json"] = extra
    _upsert_row("yt_pipeline_runs", "run_id", row)
    return run_id


# -----------------------------------------------------------------------------
# 6. yt_bot_requests (7 days TTL - Bot-to-Workflow Message Bus)
# -----------------------------------------------------------------------------
def write_bot_request(request_type: str, inputs: Dict[str, Any]) -> str:
    """Write action request from Telegram bot to the Sheets message bus."""
    req_id = f"req_{hashlib.md5(str(time.time()).encode()).hexdigest()[:8]}"
    item = {
        "request_id": req_id,
        "type": request_type,
        "inputs_json": inputs,
        "status": "pending",
        "requested_at": int(time.time()),
        "ttl": int(time.time()) + (7 * 86400),
    }
    _append_row("yt_bot_requests", item)
    return req_id


def get_bot_requests(status: str = "pending") -> List[Dict[str, Any]]:
    """Fetch bot requests from the Sheets message bus (expired ignored)."""
    out = []
    for row in _read_rows("yt_bot_requests"):
        if row.get("status") == status and not _is_expired(row):
            item = dict(row)
            item["inputs"] = item.pop("inputs_json", {}) or {}
            out.append(item)
    return out


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
    _upsert_row("yt_topics_queue", "topic_id", item)
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

    for row_number, row in _read_rows_indexed("yt_topics_queue"):
        if row.get("topic_id") == selected_id:
            row["status"] = "running"
            _update_row("yt_topics_queue", row_number, row)
            break
    selected["status"] = "running"
    return selected


def list_topics_queue(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """List queue topics filtered by status."""
    items = _read_rows("yt_topics_queue")
    if status:
        return [it for it in items if it.get("status") == status]
    return items


# Aliases for queue management
dequeue_next_topic = get_next_topic
get_topic_queue = list_topics_queue


def list_recent_videos(limit: int = 5) -> List[Dict[str, Any]]:
    """Retrieve recent video records from yt_videos."""
    items = []
    for row in _read_rows("yt_videos"):
        rec = {k: v for k, v in row.items() if k != "extra_json"}
        rec.update(row.get("extra_json") or {})
        items.append(rec)
    items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return items[:limit]


def clear_topic_queue() -> int:
    """Clear all pending topics from the topic queue."""
    rows = _read_rows("yt_topics_queue")
    kept = [r for r in rows if r.get("status") != "pending"]
    cleared = len(rows) - len(kept)
    if cleared:
        _rewrite_tab("yt_topics_queue", kept)
    return cleared


def get_latest_verification_log(video_id: str) -> Optional[Dict[str, Any]]:
    """Fetch the latest verification log for a video (expired ignored)."""
    matches = [
        v for v in _read_rows("yt_verification_log")
        if v.get("video_id") == video_id and not _is_expired(v)
    ]
    if matches:
        matches.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return matches[0]
    return None


# -----------------------------------------------------------------------------
# 8. yt_insights (YouTube landscape insights per topic)
# -----------------------------------------------------------------------------
def save_yt_insights(topic: str, insights: Any) -> str:
    """Store the ``YTInsights`` portion of a research bundle.

    *insights* may be a ``models.research.YTInsights`` instance or a
    plain dict with ``top_titles`` / ``common_tags`` / ``gaps`` lists.
    Keyed by the same topic hash as the research cache.
    """
    topic_hash = hashlib.md5(topic.lower().strip().encode("utf-8")).hexdigest()
    if hasattr(insights, "model_dump"):
        data = insights.model_dump()
    elif isinstance(insights, dict):
        data = insights
    else:
        raise ValueError("insights must be a YTInsights model or a dict")
    item = {
        "topic_hash": topic_hash,
        "topic": topic,
        "top_titles_json": data.get("top_titles", []),
        "common_tags_json": data.get("common_tags", []),
        "gaps_json": data.get("gaps", []),
        "created_at": int(time.time()),
    }
    _upsert_row("yt_insights", "topic_hash", item)
    return topic_hash


def get_yt_insights(topic: str) -> Optional[Dict[str, Any]]:
    """Retrieve stored YouTube insights for a topic, if any."""
    topic_hash = hashlib.md5(topic.lower().strip().encode("utf-8")).hexdigest()
    for row in _read_rows("yt_insights"):
        if row.get("topic_hash") == topic_hash:
            return {
                "topic_hash": row.get("topic_hash"),
                "topic": row.get("topic"),
                "top_titles": row.get("top_titles_json") or [],
                "common_tags": row.get("common_tags_json") or [],
                "gaps": row.get("gaps_json") or [],
                "created_at": row.get("created_at"),
            }
    return None
