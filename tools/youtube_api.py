# LOCAL PRODUCTION PATCH — 2026-11-25 — PR #3 (branch feat/youtube-oauth2-upload).
# Implements real YouTube OAuth2 (refresh-token flow) + resumable chunked
# upload, replacing the PR #1 placeholder that always raised
# "YouTube OAuth2 is not implemented" in _get_youtube_service().
# - _get_youtube_service() builds google.oauth2.credentials.Credentials
#   from YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN env vars,
#   refreshes the access token, and passes credentials=... (never
#   developerKey=...) to build(). Missing creds or a failed refresh
#   raise RuntimeError naming the missing variable / failure only —
#   credential VALUES are never logged, printed, or included in any
#   error message.
# - upload_private() now performs a real resumable upload via
#   request.next_chunk() in a loop (1 MiB chunks), logging progress,
#   retrying retriable HTTP 500/502/503/504 + connection errors up to
#   5 attempts with backoff, and raising RuntimeError on any other
#   API error, on an empty response, or on a simulated yt_sim_ ID.
# - No video has been uploaded to YouTube with this code yet: a live
#   Codespace run with the real stored secrets is still required
#   before upload can be proven. No credentials or tokens are
#   invented here.
"""YouTube Data API v3 integration for TubePilot AI.

Handles:
- Competitor topic analysis (search.list + videos.list)
- Private video uploads with thumbnail attachments (resumable upload)
- Publication status transitions (Private -> Public)
- Deletion / purge operations

Auth: real OAuth2 via a stored refresh token. YT_CLIENT_ID,
YT_CLIENT_SECRET and YT_REFRESH_TOKEN must be set in the environment
(e.g. GitHub Codespaces secrets). No API key is used: an API key
(developerKey) is read-only and can never authorise writes.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

__all__ = [
    "research_topic",
    "upload_private",
    "publish_public",
    "delete_video",
    "verify_video_exists",
]

# Scopes granted on the GCP OAuth consent screen (TubePilot AI):
# youtube.upload for uploads, youtube for reads / publish / delete.
_YT_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]
_YT_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Resumable-upload tuning. Chunk size must be a multiple of 256 KiB.
_UPLOAD_CHUNK_SIZE = 1024 * 1024  # 1 MiB
_MAX_UPLOAD_ATTEMPTS = 5
_RETRIABLE_UPLOAD_STATUSES = frozenset({500, 502, 503, 504})


def _get_youtube_service():
    """Build and return an authorized YouTube Resource (OAuth2).

    Reads YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN from the
    environment, builds refresh-token Credentials, refreshes the
    access token, and passes ``credentials=...`` (never
    ``developerKey=...``) to ``build("youtube", "v3", ...)``.

    Raises:
        RuntimeError: any credential is missing, the token refresh
            fails, the refreshed credentials are invalid, or the
            service cannot be built. Error messages name the missing
            variable / failure only — credential values are never
            included.
    """
    client_id = os.environ.get("YT_CLIENT_ID")
    client_secret = os.environ.get("YT_CLIENT_SECRET")
    refresh_token = os.environ.get("YT_REFRESH_TOKEN")
    missing = [
        name
        for name, value in (
            ("YT_CLIENT_ID", client_id),
            ("YT_CLIENT_SECRET", client_secret),
            ("YT_REFRESH_TOKEN", refresh_token),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "YouTube OAuth2 credentials missing from environment: "
            + ", ".join(missing)
            + ". Set YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN "
            "(e.g. as GitHub Codespaces secrets) before any YouTube "
            "upload, publish, delete or authenticated read."
        )

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=_YT_TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=_YT_SCOPES,
    )
    try:
        creds.refresh(Request())
    except RefreshError as e:
        raise RuntimeError(
            "YouTube OAuth2 token refresh failed: the stored refresh "
            "token may be expired or revoked — re-authorise and store "
            f"a fresh YT_REFRESH_TOKEN. Underlying error: {e}"
        ) from e
    except Exception as e:
        raise RuntimeError(f"YouTube OAuth2 token refresh failed: {e}") from e
    if not creds.valid:
        raise RuntimeError(
            "YouTube OAuth2 credentials are not valid after refresh; "
            "re-authorise and store a fresh YT_REFRESH_TOKEN."
        )
    try:
        return build("youtube", "v3", credentials=creds)
    except Exception as e:
        raise RuntimeError(f"Failed to build YouTube API service: {e}") from e


def _execute_resumable_upload(request, video_path: str) -> Dict[str, Any]:
    """Drive a resumable videos.insert request to completion.

    Loops ``request.next_chunk()`` until the API returns the upload
    response, logging progress. Retriable failures (HTTP 500/502/503/
    504 and connection errors) are retried up to
    ``_MAX_UPLOAD_ATTEMPTS`` with exponential backoff; every other
    API error raises immediately. A finished upload with no video ID
    raises — an empty response is never treated as success.

    Returns:
        The videos.insert response dict (must contain ``"id"``).

    Raises:
        RuntimeError: upload failed, was exhausted by retries, or
            finished without a video ID.
    """
    response = None
    attempt = 0
    while response is None:
        attempt += 1
        try:
            status, response = request.next_chunk()
        except HttpError as e:
            status_code = getattr(e.resp, "status", None)
            if (
                status_code in _RETRIABLE_UPLOAD_STATUSES
                and attempt < _MAX_UPLOAD_ATTEMPTS
            ):
                wait = min(2**attempt, 30)
                logger.warning(
                    f"YouTube upload hit retriable HTTP {status_code} "
                    f"(attempt {attempt}/{_MAX_UPLOAD_ATTEMPTS}); "
                    f"retrying in {wait}s."
                )
                time.sleep(wait)
                continue
            raise RuntimeError(
                f"YouTube upload failed for '{video_path}': "
                f"HTTP {status_code}: {e}"
            ) from e
        except (ConnectionError, TimeoutError, OSError) as e:
            if attempt < _MAX_UPLOAD_ATTEMPTS:
                wait = min(2**attempt, 30)
                logger.warning(
                    f"YouTube upload hit a connection error "
                    f"(attempt {attempt}/{_MAX_UPLOAD_ATTEMPTS}): {e}; "
                    f"retrying in {wait}s."
                )
                time.sleep(wait)
                continue
            raise RuntimeError(
                f"YouTube upload failed for '{video_path}': connection "
                f"error after {attempt} attempts: {e}"
            ) from e
        if status is not None:
            logger.info(
                f"YouTube upload progress: {int(status.progress() * 100)}%"
            )

    if not isinstance(response, dict) or not response.get("id"):
        raise RuntimeError(
            f"YouTube upload finished without a video ID in its response "
            f"for '{video_path}'; the upload cannot be treated as successful."
        )
    return response


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
def research_topic(topic: str) -> Dict[str, Any]:
    """Analyze YouTube landscape for topic: top titles, tags, and audience gaps.

    Optimized for API quota: 1 search.list unit fetches 50 video candidates.

    Returns only data actually returned by the YouTube API. Raises on
    no service, on any API exception, and on zero results — no hardcoded
    invented titles/tags/gaps are returned, and nothing is cached to
    cache/youtube/ (the old cache persisted fabricated results).
    """
    service = _get_youtube_service()  # Raises on missing creds / refresh failure.
    try:
        search_response = service.search().list(
            q=topic,
            part="id,snippet",
            maxResults=10,
            type="video",
            order="viewCount",
        ).execute()
    except Exception as e:
        raise RuntimeError(f"YouTube search API call failed for topic '{topic}': {e}") from e

    top_titles: List[str] = []
    for item in search_response.get("items", []):
        snippet = item.get("snippet", {})
        title = snippet.get("title")
        if title:
            top_titles.append(title)

    if not top_titles:
        raise RuntimeError(
            f"YouTube search returned zero results for topic '{topic}'. "
            "No invented titles/tags are returned as a fallback."
        )

    # search.list snippets do not include tags, and "gaps" require real
    # analysis of the returned videos — neither is invented here. They
    # are empty until a videos.list-based implementation populates them
    # from real API data.
    return {
        "topic": topic,
        "top_titles": top_titles,
        "common_tags": [],
        "gaps": [],
        "source": "youtube_search_api",
    }

def upload_private(video_path: str, thumb_path: Optional[str], seo_data: Dict[str, Any]) -> str:
    """Upload video to YouTube set to PRIVATE status, via resumable upload.

    Returns:
        yt_video_id (str) — the real ID returned by the YouTube API.

    Raises:
        FileNotFoundError: video file does not exist.
        RuntimeError: no authenticated service (missing OAuth2 creds /
            failed token refresh), any upload API error, an upload
            that finishes without a video ID, or a simulated ID.
            No fake yt_sim_ ID is ever returned.
    """
    p = Path(video_path)
    if not p.exists():
        raise FileNotFoundError(f"Video file not found at: {video_path}")

    logger.info(f"Uploading private video to YouTube: {seo_data.get('title')}")
    # Raises RuntimeError when unauthenticated — deliberately outside
    # any try/except so it is never swallowed.
    service = _get_youtube_service()

    body = {
        "snippet": {
            "title": seo_data.get("title", "TubePilot Deep Dive")[:100],
            "description": seo_data.get("description", ""),
            "tags": seo_data.get("tags", []),
            "categoryId": "27",  # Education
        },
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        video_path, chunksize=_UPLOAD_CHUNK_SIZE, resumable=True
    )
    request = service.videos().insert(
        part="snippet,status", body=body, media_body=media
    )
    # Raises RuntimeError on any API error / empty response.
    response = _execute_resumable_upload(request, video_path)
    video_id = response.get("id")

    if str(video_id).startswith("yt_sim_"):
        raise RuntimeError(f"YouTube upload returned a simulated video ID ({video_id}); refusing it.")

    if thumb_path and Path(thumb_path).exists() and video_id:
        try:
            thumb_media = MediaFileUpload(thumb_path)
            service.thumbnails().set(videoId=video_id, media_body=thumb_media).execute()
            logger.info("Thumbnail successfully applied to YouTube video.")
        except Exception as te:
            logger.warning(f"Could not upload thumbnail: {te}")

    return video_id


def verify_video_exists(yt_video_id: str) -> bool:
    """Confirm a video ID really exists on YouTube (videos.list).

    Returns True only if the API returns a video with the same ID.
    Raises on no service / any API exception — a failed check is never
    treated as "verified". Callers must not purge local media unless
    this returns True.
    """
    if not yt_video_id or str(yt_video_id).startswith(("yt_sim_", "yt_priv_")):
        raise ValueError(
            f"Refusing to verify simulated video ID '{yt_video_id}': "
            "only real YouTube IDs can be verified."
        )
    # Raises RuntimeError when unauthenticated.
    service = _get_youtube_service()
    response = service.videos().list(part="id", id=yt_video_id).execute()
    items = response.get("items", [])
    return any(item.get("id") == yt_video_id for item in items)


def publish_public(yt_video_id: str) -> bool:
    """Change video privacyStatus from PRIVATE to PUBLIC.

    Raises when unauthenticated. Returns False
    only for a real API failure.
    """
    logger.info(f"Publishing YouTube video {yt_video_id} to PUBLIC.")
    # Raises RuntimeError when unauthenticated — deliberately outside
    # the try/except so it is never converted into a False/True.
    service = _get_youtube_service()
    try:
        service.videos().update(
            part="status",
            body={"id": yt_video_id, "status": {"privacyStatus": "public"}},
        ).execute()
        return True
    except Exception as e:
        logger.error(f"Failed to publish video {yt_video_id}: {e}")
        return False


def delete_video(yt_video_id: str) -> bool:
    """Delete video from YouTube.

    Raises when unauthenticated. Returns False
    only for a real API failure.
    """
    logger.info(f"Deleting YouTube video {yt_video_id}.")
    # Raises RuntimeError when unauthenticated — deliberately outside
    # the try/except so it is never converted into a False/True.
    service = _get_youtube_service()
    try:
        service.videos().delete(id=yt_video_id).execute()
        return True
    except Exception as e:
        logger.error(f"Failed to delete video {yt_video_id}: {e}")
        return False
