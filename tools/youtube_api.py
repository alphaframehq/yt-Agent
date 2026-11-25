# OAuth2 implementation — 2026-11-25 — PR #3 (branch feat/youtube-oauth2).
# Source: alphaframehq/yt-Agent @ main 35d2ee904d8572e1cc4d5acd58cc0ebb8807e743
# (PR #1 merged 8212f762 + PR #2 merged d71e15e8 + ci.yml 35d2ee9).
# Closes audit P0 #13/#14/#15 and docs/GAPS.md #1:
# - _get_youtube_service() now builds REAL OAuth2 user credentials from
#   YT_CLIENT_ID + YT_CLIENT_SECRET + YT_REFRESH_TOKEN (env only, never
#   hardcoded, never logged), refreshes the access token as needed, and
#   passes credentials=... (never developerKey=...) to build().
#   The old code passed YT_CLIENT_SECRET as developerKey — an OAuth
#   client secret is NOT an API key, and an API key is read-only and can
#   never authorise uploads / publish / delete.
# - upload_private() uses a real resumable videos.insert and returns
#   only the real video ID from the API response. Title is REQUIRED from
#   seo_data (never an invented default); description/tags come from
#   seo_data only. No yt_sim_ / yt_priv_ ID is ever minted or accepted.
# - publish_public() / delete_video() are real API calls (were already
#   wired in PR #1; they now work because the service is real).
# - verify_video_exists() is a real videos.list check (PR #1).
# - Fail-loud contract kept: missing env vars, expired/revoked refresh
#   token (invalid_grant), refresh failure, or any upload/API error
#   raises RuntimeError/ValueError — never a fake success.
# - No secret/key/token value appears in this file — env-var names only.
# - Testing-mode warning: a refresh token from an app in Testing
#   publishing status expires after 7 days (this token ~2 Dec 2026).
#   See docs/SETUP.md §1a for publish / re-issue. Expiry is NOT
#   handled by inventing a token — refresh failure raises loudly.
"""YouTube Data API v3 integration for TubePilot AI.

Handles:
- Competitor topic analysis (search.list + videos.list)
- Private video uploads with thumbnail attachments
- Publication status transitions (Private -> Public)
- Deletion / purge operations

Auth: OAuth2 user credentials (refresh-token flow) for channel
Alpha Frame. Requires env vars YT_CLIENT_ID, YT_CLIENT_SECRET and
YT_REFRESH_TOKEN — see docs/SETUP.md §1/§7. No API key is used for
writes; an API key (developerKey) is read-only and cannot authorise
uploads, publish or delete.
"""

from __future__ import annotations

import os
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

# OAuth2 configuration — env-var names and public endpoints only.
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]
_ENV_VARS = ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")
# YouTube category "Education" (27) — a platform category ID, not
# invented content. The pipeline only produces education/finance
# explainers, so this is the correct category for its uploads.
_CATEGORY_ID_EDUCATION = "27"


def _get_youtube_service():
    """Build and return an authorized YouTube Resource (real OAuth2).

    Reads YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN from the
    environment, builds google.oauth2.credentials.Credentials with the
    stored refresh token, refreshes the access token, and returns
    build("youtube", "v3", credentials=...).

    Raises:
        RuntimeError: any YT_* env var is missing/empty; the Google
            client libraries are not installed; the refresh fails
            (e.g. invalid_grant — expired/revoked refresh token, which
            in Testing publishing status happens after 7 days); or the
            credentials are not valid after refresh. The exception
            message names the missing var / failure kind, never any
            credential value. Callers must re-issue the refresh token
            (docs/SETUP.md §1a) — never invent or reuse a fake token.
    """
    missing = [name for name in _ENV_VARS if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError(
            "YouTube OAuth2 credentials missing: "
            + ", ".join(missing)
            + " not set (or empty) in the environment. Set all three "
            "as GitHub Codespaces secrets / .env entries — see "
            "docs/SETUP.md §1 and §7. No upload, publish, delete or "
            "authenticated read can run without them."
        )

    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as e:
        raise RuntimeError(
            "YouTube client libraries are not installed "
            "(google-api-python-client and google-auth-oauthlib are in "
            "requirements.txt). Run: pip install -r requirements.txt"
        ) from e

    creds = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"].strip(),
        token_uri=_TOKEN_URI,
        client_id=os.environ["YT_CLIENT_ID"].strip(),
        client_secret=os.environ["YT_CLIENT_SECRET"].strip(),
        scopes=_SCOPES,
    )
    try:
        creds.refresh(Request())
    except RefreshError as e:
        raise RuntimeError(
            "YouTube OAuth refresh failed: the stored refresh token "
            "was rejected by Google (invalid_grant — expired, revoked "
            "or issued for a different client). Re-issue a fresh "
            "YT_REFRESH_TOKEN and store it (docs/SETUP.md §1a/§1b); "
            "in Testing publishing status a refresh token expires "
            "after 7 days. Underlying error: "
            + str(e)
        ) from e
    except Exception as e:
        raise RuntimeError(
            "YouTube OAuth refresh failed while obtaining an access "
            f"token: {e}. Check the stored YT_* credentials and "
            "network access to oauth2.googleapis.com."
        ) from e

    if not getattr(creds, "valid", False):
        raise RuntimeError(
            "YouTube OAuth credentials are not valid after refresh; "
            "no access token was obtained. Re-issue YT_REFRESH_TOKEN "
            "(docs/SETUP.md §1a)."
        )

    # cache_discovery=False: do not write a discovery-doc cache to disk.
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
def research_topic(topic: str) -> Dict[str, Any]:
    """Analyze YouTube landscape for topic: top titles, tags, and audience gaps.

    Optimized for API quota: 1 search.list unit fetches 50 video candidates.

    Returns only data actually returned by the YouTube API. Raises on
    no service, on any API exception, and on zero results — no hardcoded
    invented titles/tags/gaps are returned, and nothing is cached to
    cache/youtube/ (the old cache persisted fabricated results).
    """
    service = _get_youtube_service()  # Raises on missing/revoked OAuth creds.
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
    """Upload video to YouTube set to PRIVATE status (real resumable upload).

    Title/description/tags come ONLY from seo_data (the script bundle's
    SEO, produced by the LLM from the real research bundle). The title
    is required — there is no invented default title.

    Returns:
        yt_video_id (str) — the real ID returned by the YouTube API.

    Raises:
        FileNotFoundError: video file does not exist.
        ValueError: seo_data has no non-empty title, or the API
            returned a simulated ID.
        RuntimeError: no authenticated service (missing/revoked OAuth
            creds) or any upload exception. No fake yt_sim_ ID is
            returned.
    """
    p = Path(video_path)
    if not p.exists():
        raise FileNotFoundError(f"Video file not found at: {video_path}")

    title = str(seo_data.get("title") or "").strip()
    if not title:
        raise ValueError(
            "Refusing upload: seo_data has no title. The video title "
            "must come from the script bundle's SEO — no default title "
            "is invented here."
        )
    description = str(seo_data.get("description") or "")[:5000]
    tags = [str(t).strip() for t in (seo_data.get("tags") or []) if str(t).strip()]
    # YouTube allows at most 500 characters of tags in total.
    _total = 0
    _kept_tags: List[str] = []
    for tag in tags:
        if _total + len(tag) + 1 > 500:
            break
        _kept_tags.append(tag[:30])
        _total += len(tag) + 1

    logger.info(f"Uploading private video to YouTube: {title}")
    # Raises RuntimeError when unauthenticated — deliberately outside
    # any try/except so it is never swallowed.
    service = _get_youtube_service()

    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": _kept_tags,
            "categoryId": _CATEGORY_ID_EDUCATION,
        },
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)
    try:
        # Resumable upload: poll next_chunk() until the API returns the
        # completed response (which carries the real video ID).
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status is not None:
                logger.info(f"Upload progress: {int(status.progress() * 100)}%")
    except Exception as e:
        raise RuntimeError(f"YouTube upload failed for '{title}': {e}") from e

    if not isinstance(response, dict):
        raise RuntimeError("YouTube upload returned no response object.")
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
    # Raises RuntimeError when unauthenticated.
    service = _get_youtube_service()
    response = service.videos().list(part="id", id=yt_video_id).execute()
    items = response.get("items", [])
    return any(item.get("id") == yt_video_id for item in items)


def publish_public(yt_video_id: str) -> bool:
    """Change video privacyStatus from PRIVATE to PUBLIC.

    Raises when unauthenticated. Returns False only for a real API
    failure.
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

    Raises when unauthenticated. Returns False only for a real API
    failure.
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
