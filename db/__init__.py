"""Google Sheets database layer for TubePilot AI.

Replaces the DynamoDB layer (25 Nov 2026): all non-secret data lives
in one Google Spreadsheet (see ``db/sheets.py``); secrets live in
GitHub repo secrets / env vars (see ``tools/secrets.py``).
"""

from db.sheets import (
    cache_research,
    clear_topic_queue,
    enqueue_topic,
    get_bot_requests,
    get_cached_research,
    get_latest_verification_log,
    get_next_topic,
    get_short_record,
    get_trend_report,
    get_video_record,
    get_yt_insights,
    list_recent_shorts,
    list_recent_videos,
    list_topics_queue,
    log_verification,
    record_pipeline_run,
    save_short_record,
    save_trend_report,
    save_video_record,
    save_yt_insights,
    update_short_status,
    update_video_status,
    write_bot_request,
)
from db.setup_sheets import setup_all_sheets

__all__ = [
    "setup_all_sheets",
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
    "save_trend_report",
    "get_trend_report",
    "save_short_record",
    "get_short_record",
    "update_short_status",
    "list_recent_shorts",
]
