# LOCAL PRODUCTION PATCH — 2026-11-25 — NOT pushed to GitHub.
# Source: alphaframehq/yt-Agent @ main c39f42541b8e784dcf41c09583dc57227be07667
# Fixes audit P0 #13/#14/#15 (task slice #14/#15/#16/#17):
# - upload_private() raises on no service / any upload exception; no
#   yt_sim_ fake IDs are ever returned.
# - publish_public()/delete_video() raise when unauthenticated and return
#   False only for a real API failure.
# - _get_youtube_service() NO LONGER passes YT_CLIENT_SECRET as
#   developerKey: YT_CLIENT_SECRET is an OAuth client secret, NOT an API
#   key, and an API key cannot authorise writes. Real OAuth2 (stored
#   refresh token, YT_REFRESH_TOKEN env) is UNIMPLEMENTED and required —
#   the function raises stating exactly that. No OAuth credentials or
#   tokens are invented here.
# - research_topic() raises on no service / API exception / zero results,
#   returns only real titles, and no longer writes/reads fabricated
#   results to cache/youtube/ (the old cache persisted invented titles).
# - New verify_video_exists() helper (real videos.list check) so callers
#   can confirm a YouTube copy exists before purging local media.
"""YouTube Data API v3 integration for TubePilot AI.

Handles:
- Competitor topic analysis (search.list + videos.list)
- Private video uploads with thumbnail attachments
- Publication status transitions (Private -> Public)
- Deletion / purge operations
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

__all__ = [
    "research_topic",
    "upload_private",
    "publish_public",
    "delete_video",
    "verify_video_exists",
]


def _get_youtube_service():
    """Build and return an authorized YouTube Resource.

    Raises:
        RuntimeError: always, until real OAuth2 is implemented. The
        original code passed YT_CLIENT_SECRET as ``developerKey`` to
        ``build("youtube", "v3", developerKey=client_secret)`` — that is
        wrong in two ways: (1) YT_CLIENT_SECRET is an OAuth client
        secret, NOT an API key, and must never be sent as developerKey;
        (2) an API key (developerKey) is read-only and can never
        authorise uploads / publish / delete, which require OAuth2 user
        credentials. Real OAuth2 is UNIMPLEMENTED: it requires building
        google.oauth2.credentials.Credentials from YT_CLIENT_ID,
        YT_CLIENT_SECRET and a stored refresh token (YT_REFRESH_TOKEN
        env / secrets store — placeholder name only, no value here),
        refreshing it as needed, and passing ``credentials=...`` (never
        ``developerKey=...``) to build(). Implement that here and remove
        this raise; do not invent credentials or fake a token.
    """
    raise RuntimeError(
        "YouTube OAuth2 is not implemented: uploads, publish, delete "
        "and authenticated reads require OAuth2 user credentials "
        "(YT_CLIENT_ID + YT_CLIENT_SECRET + stored refresh token "
        "YT_REFRESH_TOKEN). YT_CLIENT_SECRET must NOT be used as an API "
        "key (developerKey) — it is an OAuth client secret, and an API "
        "key cannot authorise writes. Implement real OAuth2 in "
        "_get_youtube_service() before using any write operation."
    )


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
def research_topic(topic: str) -> Dict[str, Any]:
    """Analyze YouTube landscape for topic: top titles, tags, and audience gaps.

    Optimized for API quota: 1 search.list unit fetches 50 video candidates.

    Returns only data actually returned by the YouTube API. Raises on
    no service, on any API exception, and on zero results — no hardcoded
    invented titles/tags/gaps are returned, and nothing is cached to
    cache/youtube/ (the old cache persisted fabricated results).
    """
    service = _get_youtube_service()  # Raises: OAuth2 unimplemented.
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
    """Upload video to YouTube set to PRIVATE status.

    Returns:
        yt_video_id (str) — the real ID returned by the YouTube API.

    Raises:
        FileNotFoundError: video file does not exist.
        RuntimeError: no authenticated service (OAuth2 unimplemented)
            or any upload exception. No fake yt_sim_ ID is returned.
    """
    p = Path(video_path)
    if not p.exists():
        raise FileNotFoundError(f"Video file not found at: {video_path}")

    logger.info(f"Uploading private video to YouTube: {seo_data.get('title')}")
    # Raises RuntimeError when unauthenticated / OAuth2 unimplemented —
    # deliberately outside any try/except so it is never swallowed.
    service = _get_youtube_service()

    from googleapiclient.http import MediaFileUpload

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

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)
    response = request.execute()  # Any exception propagates to the caller.
    video_id = response.get("id")

    if not video_id:
        raise RuntimeError("YouTube upload returned no video ID in its response.")
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
    # Raises RuntimeError when unauthenticated / OAuth2 unimplemented.
    service = _get_youtube_service()
    response = service.videos().list(part="id", id=yt_video_id).execute()
    items = response.get("items", [])
    return any(item.get("id") == yt_video_id for item in items)


def publish_public(yt_video_id: str) -> bool:
    """Change video privacyStatus from PRIVATE to PUBLIC.

    Raises when unauthenticated (OAuth2 unimplemented). Returns False
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

    Raises when unauthenticated (OAuth2 unimplemented). Returns False
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
