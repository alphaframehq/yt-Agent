"""YouTube Uploader and Approval Dispatch Agent for TubePilot AI.

Workflow:
1. Upload video to YouTube with privacyStatus="private"
2. Attach branded thumbnail
3. Record permanent metadata into DynamoDB yt_videos table
4. Execute ephemeral purge: delete local video and thumbnail files
5. Prepare Telegram approval notification payload with 5 action buttons:
   [✅ Publish] [❌ Reject] [🔁 Regenerate] [✏️ Edit] [🖼️ Thumbnail]
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional
from loguru import logger
from pydantic import BaseModel, Field

from db.dynamo import save_video_record
from models.script import VideoScript
from storage.ephemeral import purge_local_media
from tools.youtube_api import upload_private

__all__ = [
    "UploadResult",
    "UploaderAgent",
    "run_upload",
]


class UploadResult(BaseModel):
    """Result of YouTube private upload and DynamoDB registration."""
    video_id: str
    yt_video_id: str
    yt_url: str
    status: str = "pending_approval"
    title: str
    purged_local_bytes: int = 0
    approval_buttons: list[list[str]] = Field(default_factory=list)


class UploaderAgent:
    """Manages secure private video publishing, DynamoDB persistence, and local disk wiping."""

    def __init__(self):
        pass

    def upload_and_register(
        self,
        video_path: str,
        thumb_path: Optional[str],
        script: VideoScript,
        cost_usd: float = 0.0,
    ) -> UploadResult:
        """Upload video privately, record in DynamoDB, purge local files, and return approval payload."""
        v_path = Path(video_path)
        if not v_path.exists():
            raise FileNotFoundError(f"Video to upload not found: {video_path}")

        file_size = v_path.stat().st_size
        logger.info(f"Uploading private video to YouTube ({file_size} bytes): '{script.seo.title}'")

        # 1. YouTube API private upload
        seo_dict = script.seo.model_dump()
        yt_video_id = upload_private(str(v_path.resolve()), thumb_path, seo_dict)
        yt_url = f"https://youtu.be/{yt_video_id}"

        # 2. Save metadata to DynamoDB yt_videos
        video_record = {
            "pillar": script.pillar,
            "topic": script.seo.title,
            "yt_video_id": yt_video_id,
            "yt_url": yt_url,
            "title": script.seo.title,
            "description_preview": script.seo.description[:200],
            "tags": script.seo.tags,
            "thumbnail_url": thumb_path or "",
            "script_json": script.model_dump_json(),
            "verification_score": 1.0,
            "duration_sec": script.meta.estimated_duration_sec,
            "cost_usd": cost_usd,
            "status": "pending_approval",
            "created_at": int(time.time()),
        }
        db_video_id = save_video_record(video_record)

        # 3. Purge local video and thumbnail files (YouTube IS the permanent storage)
        purge_local_media(video_path, thumb_path)
        logger.info(f"Ephemeral purge completed. Local video {v_path.name} deleted.")

        # 4. Telegram approval buttons payload
        buttons = [
            ["✅ Publish", f"pub_{yt_video_id}"],
            ["❌ Reject", f"rej_{yt_video_id}"],
            ["🔁 Regenerate", f"reg_{db_video_id}"],
            ["✏️ Edit", f"edt_{db_video_id}"],
            ["🖼️ Thumbnail", f"thm_{yt_video_id}"],
        ]

        return UploadResult(
            video_id=db_video_id,
            yt_video_id=yt_video_id,
            yt_url=yt_url,
            status="pending_approval",
            title=script.seo.title,
            purged_local_bytes=file_size,
            approval_buttons=buttons,
        )


def run_upload(
    video_path: str,
    thumb_path: Optional[str],
    script: VideoScript,
    cost_usd: float = 0.0,
) -> UploadResult:
    """Convenience functional interface for video uploading and registration."""
    return UploaderAgent().upload_and_register(video_path, thumb_path, script, cost_usd)
