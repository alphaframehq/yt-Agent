"""DynamoDB database layer for TubePilot AI."""

from db.dynamo import (
    cache_research,
    enqueue_topic,
    get_bot_requests,
    get_cached_research,
    get_next_topic,
    get_video_record,
    list_topics_queue,
    log_verification,
    record_pipeline_run,
    save_video_record,
    update_video_status,
    write_bot_request,
)
from db.setup_dynamo import setup_all_tables

__all__ = [
    "setup_all_tables",
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
