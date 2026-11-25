# LOCAL PRODUCTION PATCH — 2026-11-25 — Applied via PR #1 (branch fix/production-mocks), 2026-11-25.
# Source: alphaframehq/yt-Agent @ main c39f42541b8e784dcf41c09583dc57227be07667
# Fixes audit P0 #12 (task slice #13): upload_and_register() now REQUIRES
# a passing VerificationReport (real score from report.confidence, never
# the old hardcoded 1.0), refuses on a fail verdict, rejects files below
# a minimum size/duration or failing an ffprobe probe, and does NOT purge
# local media until the YouTube copy is verified via videos.list
# (tools.youtube_api.verify_video_exists) AND the DynamoDB record is
# saved. Empty/missing thumbnails warn (no thumbnail) or raise (supplied
# but missing/empty) instead of being silently recorded as "".
# BREAKING CHANGE: signature changed — verification_report is a required
# argument. Callers are updated: orchestration/graph.py node_finalize
# now calls the uploader and passes the real VerificationReport from
# node_verify (raises if missing or not "pass"); never fabricate a
# report. run_upload() changed the same way.
"""YouTube Uploader and Approval Dispatch Agent for TubePilot AI.

Workflow:
1. Require a passing VerificationReport; refuse on fail
2. Validate the local video (exists, minimum size, ffprobe duration probe)
3. Upload video to YouTube with privacyStatus="private"
4. Verify the YouTube copy exists before touching local files
5. Attach branded thumbnail
6. Record permanent metadata into DynamoDB yt_videos table
7. Only then execute ephemeral purge: delete local video and thumbnail files
8. Prepare Telegram approval notification payload with 5 action buttons:
   [✅ Publish] [❌ Reject] [🔁 Regenerate] [✏️ Edit] [🖼️ Thumbnail]
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional
from loguru import logger
from pydantic import BaseModel, Field

from db.dynamo import save_video_record
from models.script import VideoScript
from models.verification import VerificationReport
from storage.ephemeral import purge_local_media
from tools.youtube_api import upload_private, verify_video_exists

__all__ = [
    "UploadResult",
    "UploaderAgent",
    "run_upload",
]

# Conservative minima: the old placeholder MP4 was ~40 bytes with no
# frames. These are sanity floors, not a quality bar — a real render is
# orders of magnitude larger/longer.
MIN_VIDEO_BYTES = 10_000
MIN_DURATION_SEC = 1.0


class UploadResult(BaseModel):
    """Result of YouTube private upload and DynamoDB registration."""
    video_id: str
    yt_video_id: str
    yt_url: str
    status: str = "pending_approval"
    title: str
    purged_local_bytes: int = 0
    approval_buttons: list[list[str]] = Field(default_factory=list)


def _probe_duration_sec(video_path: Path) -> float:
    """Return the real duration of *video_path* via ffprobe.

    Raises if ffprobe is unavailable, the probe fails, or no duration
    is reported. A video that cannot be probed is never uploaded.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "ffprobe is not available, so the video cannot be probed "
            "before upload. Install ffmpeg/ffprobe; unverified files "
            "are never uploaded."
        ) from e
    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {video_path}: {result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
        raise RuntimeError(f"ffprobe reported no usable duration for {video_path}: {e}") from e
    if duration <= 0:
        raise RuntimeError(f"ffprobe reported a non-positive duration ({duration}s) for {video_path}.")
    return duration


class UploaderAgent:
    """Manages secure private video publishing, DynamoDB persistence, and local disk wiping."""

    def __init__(self):
        pass

    def upload_and_register(
        self,
        video_path: str,
        thumb_path: Optional[str],
        script: VideoScript,
        verification_report: VerificationReport,
        cost_usd: float = 0.0,
    ) -> UploadResult:
        """Upload video privately, record in DynamoDB, purge local files, and return approval payload.

        Args:
            verification_report: REQUIRED — the real VerificationReport
                from agents.verify.run_verification() for this script.
                Upload is refused unless report.overall == "pass" and a
                real numeric report.confidence is present. The recorded
                verification_score is report.confidence, never a
                hardcoded value. Callers (orchestration/graph.py
                node_finalize) must pass the real report; do not call
                this with a fabricated report or with None.

        Local files are purged ONLY after the YouTube copy has been
        verified (videos.list) AND the DynamoDB record is saved. Any
        failure before that point raises and leaves local files intact.
        """
        # 0. Verification gate — fail verdicts never reach YouTube.
        if verification_report is None:
            raise ValueError(
                "upload_and_register() requires a VerificationReport; "
                "got None. Pass the real report from run_verification()."
            )
        overall = getattr(verification_report, "overall", None)
        confidence = getattr(verification_report, "confidence", None)
        if overall != "pass":
            raise ValueError(
                f"Refusing upload: verification verdict is '{overall}', "
                "not 'pass'. Fix the reported issues and re-verify; "
                "a failed script must never be uploaded."
            )
        if confidence is None or not isinstance(confidence, (int, float)):
            raise ValueError(
                "Refusing upload: VerificationReport has no real numeric "
                f"confidence score (got {confidence!r}). The recorded "
                "verification_score must come from the real report."
            )
        verification_score = float(confidence)

        # 0b. Local video validation — existence, size, probe.
        v_path = Path(video_path)
        if not v_path.exists():
            raise FileNotFoundError(f"Video to upload not found: {video_path}")

        file_size = v_path.stat().st_size
        if file_size < MIN_VIDEO_BYTES:
            raise ValueError(
                f"Refusing upload: video is only {file_size} bytes "
                f"(minimum {MIN_VIDEO_BYTES}); this looks like an empty "
                "placeholder render, not a real video."
            )
        duration_sec = _probe_duration_sec(v_path)
        if duration_sec < MIN_DURATION_SEC:
            raise ValueError(
                f"Refusing upload: probed duration is {duration_sec}s "
                f"(minimum {MIN_DURATION_SEC}s)."
            )
        logger.info(f"Uploading private video to YouTube ({file_size} bytes, {duration_sec}s): '{script.seo.title}'")

        # 0c. Thumbnail validation — empty is warned/rejected, never "".
        thumbnail_url: Optional[str] = None
        if thumb_path:
            t_path = Path(thumb_path)
            if not t_path.exists():
                raise FileNotFoundError(f"Thumbnail supplied but not found: {thumb_path}")
            if t_path.stat().st_size == 0:
                raise ValueError(f"Refusing upload: thumbnail is a 0-byte file: {thumb_path}")
            thumbnail_url = str(t_path.resolve())
        else:
            logger.warning(
                "No thumbnail supplied for upload; proceeding without one. "
                "thumbnail_url will be recorded as null, not as an empty string."
            )

        # 1. YouTube API private upload (raises on any failure; no fake ID).
        seo_dict = script.seo.model_dump()
        yt_video_id = upload_private(str(v_path.resolve()), thumb_path, seo_dict)
        yt_url = f"https://youtu.be/{yt_video_id}"

        # 1b. Verify the YouTube copy exists BEFORE any purge or DB write
        # that would treat the upload as done. Raises on failure.
        if not verify_video_exists(yt_video_id):
            raise RuntimeError(
                f"YouTube copy of uploaded video {yt_video_id} could not "
                "be verified via videos.list; local files are NOT purged "
                "and no record is saved."
            )
        logger.info(f"YouTube copy verified for video {yt_video_id}.")

        # 2. Save metadata to DynamoDB yt_videos
        video_record = {
            "pillar": script.pillar,
            "topic": script.seo.title,
            "yt_video_id": yt_video_id,
            "yt_url": yt_url,
            "title": script.seo.title,
            "description_preview": script.seo.description[:200],
            "tags": script.seo.tags,
            "thumbnail_url": thumbnail_url,
            "script_json": script.model_dump_json(),
            "verification_score": verification_score,
            "verification_overall": overall,
            "duration_sec": duration_sec,
            "cost_usd": cost_usd,
            "status": "pending_approval",
            "created_at": int(time.time()),
        }
        db_video_id = save_video_record(video_record)

        # 3. Purge local video and thumbnail files ONLY now that the
        # YouTube copy is verified and the DB record is saved. If purge
        # itself raises, the error propagates and the result is not
        # returned as a successful upload.
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
    verification_report: VerificationReport,
    cost_usd: float = 0.0,
) -> UploadResult:
    """Convenience functional interface for video uploading and registration.

    verification_report is required (see UploaderAgent.upload_and_register):
    pass the real VerificationReport from run_verification().
    """
    return UploaderAgent().upload_and_register(video_path, thumb_path, script, verification_report, cost_usd)
