# OAuth2 implementation — 2026-11-25 — PR #4 (branch feat/youtube-oauth2).
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
- Description updates for internal linking (videos.update)
- Deletion / purge operations

Auth: OAuth2 user credentials (refresh-token flow) for channel
Alpha Frame. Requires env vars YT_CLIENT_ID, YT_CLIENT_SECRET and
YT_REFRESH_TOKEN — see docs/SETUP.md §1/§7. No API key is used for
writes; an API key (developerKey) is read-only and cannot authorise
uploads, publish or delete.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

_T = TypeVar("_T")

# 26 Nov 2026: there is no PR #3 in this repo's history (merges are
# #1, #2, #4–#8), and only research_topic() had any retry — a bare
# @retry that retried EVERYTHING, including missing credentials.
# The retry/backoff below is added here, for all five YouTube API
# entry points, and it retries TRANSIENT failures only (5xx / 429 /
# timeouts / connection resets). Auth failures (401/403,
# invalid_grant, missing YT_* env vars) are permanent and are never
# retried: retrying them only burns quota and delays the loud error
# that tells the operator to re-issue the refresh token.
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "429", "500", "502", "503", "504",
    "rate limit", "rate-limit", "ratelimit",
    "too many requests", "timeout", "timed out",
    "connection reset", "connection refused", "connection aborted",
    "service unavailable", "internal server error", "internal error",
    "backend error", "bad gateway", "gateway timeout",
    "temporarily unavailable", "eof occurred", "broken pipe",
)
_PERMANENT_MARKERS: tuple[str, ...] = (
    "invalid_grant", "invalid grant", "credentials missing",
    "not set (or empty)", "not set in the environment",
    "unauthorized", "forbidden", "invalid credentials",
    "401", "403", "404",
)


def _is_transient_youtube_error(exc: BaseException) -> bool:
    """Return True only for a retryable YouTube API failure."""
    msg = str(exc).lower()
    if any(marker in msg for marker in _PERMANENT_MARKERS):
        return False
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


def _youtube_retry(fn: Callable[..., _T]) -> Callable[..., _T]:
    """Tenacity retry for raising YouTube calls: 3 attempts, exp backoff."""
    return retry(
        retry=retry_if_exception(_is_transient_youtube_error),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )(fn)

__all__ = [
    "research_topic",
    "upload_private",
    "upload_short",
    "shorts_seo",
    "publish_public",
    "update_video_description",
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


@_youtube_retry
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


@_youtube_retry
def upload_private(video_path: str, thumb_path: Optional[str], seo_data: Dict[str, Any]) -> str:
    """Upload video to YouTube set to PRIVATE status (real resumable upload).

    Retried (transient failures only, 3 attempts) via @_youtube_retry:
    a retried upload starts a fresh resumable session — the abandoned
    partial upload never completes, so no duplicate video is created.

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


def shorts_seo(seo_data: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of *seo_data* adjusted for a YouTube Short.

    Pure function (no API call), so it is unit-testable:

    - ``#Shorts`` is added to the title (once) and to the start of
      the description (once) — YouTube treats #Shorts in the title
      or description as the Shorts signal.
    - The title is kept within YouTube's 100-character limit: if
      appending " #Shorts" would overflow, the base title is
      truncated first. The title itself is never invented here — it
      comes from the Shorts script's SEO.
    - A "shorts" tag is added (once) so Shorts stay discoverable
      alongside the long video's tags.
    """
    seo = dict(seo_data or {})
    title = str(seo.get("title") or "").strip()
    if not title:
        raise ValueError(
            "Refusing Shorts SEO: seo_data has no title. The title must "
            "come from the Shorts script's SEO — no default is invented."
        )
    if "#Shorts" not in title:
        suffix = " #Shorts"
        title = (title[: 100 - len(suffix)] + suffix) if len(title) + len(suffix) > 100 else title + suffix
    seo["title"] = title
    description = str(seo.get("description") or "")
    if "#Shorts" not in description:
        seo["description"] = "#Shorts\n\n" + description
    tags = [str(t).strip() for t in (seo.get("tags") or []) if str(t).strip()]
    if "shorts" not in [t.lower() for t in tags]:
        tags.append("shorts")
    seo["tags"] = tags
    return seo


def upload_short(video_path: str, thumb_path: Optional[str], seo_data: Dict[str, Any]) -> str:
    """Upload a vertical 9:16 video as a YouTube Short (PRIVATE).

    Separate upload path from the long-form ``upload_private``: the
    SEO is passed through :func:`shorts_seo` first, so every Short
    carries ``#Shorts`` in its title and description. The video file
    itself must already be a vertical render (``agents.assemble.
    assemble_shorts`` renders 1080x1920); this function does not
    re-encode or crop — a non-vertical file is the assembler's
    responsibility, not the uploader's silent fix.

    Returns the real YouTube video ID; raises exactly like
    ``upload_private`` on any failure (no fake ID, ever).
    """
    return upload_private(video_path, thumb_path, shorts_seo(seo_data))


@_youtube_retry
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
    failure. Transient failures (5xx/429/timeout) are retried
    internally (3 attempts, exponential backoff) before giving up
    with False — the False contract for callers is unchanged.
    """
    logger.info(f"Publishing YouTube video {yt_video_id} to PUBLIC.")
    # Raises RuntimeError when unauthenticated — deliberately outside
    # the try/except so it is never converted into a False/True.
    service = _get_youtube_service()
    for attempt in range(1, 4):
        try:
            service.videos().update(
                part="status",
                body={"id": yt_video_id, "status": {"privacyStatus": "public"}},
            ).execute()
            return True
        except Exception as e:
            if _is_transient_youtube_error(e) and attempt < 3:
                delay = min(2 ** attempt, 10)
                logger.warning(
                    f"Publish failed transiently ({e}); retrying in "
                    f"{delay}s (attempt {attempt}/3)."
                )
                time.sleep(delay)
                continue
            logger.error(f"Failed to publish video {yt_video_id}: {e}")
            return False
    return False


def update_video_description(yt_video_id: str, description: str) -> bool:
    """Replace a video's description, preserving title/tags/category.

    Internal-linking step (26 Nov 2026): Shorts video IDs exist only
    AFTER upload, so the long video's description cannot link them at
    upload time. The daily runner uploads the long video first, then
    the Shorts, then calls this (via
    ``agents/uploader.cross_link_daily_uploads``) to add the Shorts
    links to the long video's description.

    The current snippet is fetched first (videos.list) so title,
    tags and categoryId are preserved — videos.update replaces the
    whole snippet, and sending only a description would drop them.
    The description is truncated to YouTube's 5000-character limit.

    Returns True on success, False on a real API failure (the caller
    logs and continues — the videos exist; only the links are
    missing). Raises on unauthenticated service, simulated IDs, or
    a video ID the API does not know. Transient failures are retried
    internally (3 attempts, exponential backoff).
    """
    if not yt_video_id or str(yt_video_id).startswith(("yt_sim_", "yt_priv_")):
        raise ValueError(
            f"Refusing to update description for simulated video ID "
            f"'{yt_video_id}': only real YouTube IDs can be updated."
        )
    description = str(description or "")[:5000]
    logger.info(f"Updating description for YouTube video {yt_video_id}.")
    # Raises RuntimeError when unauthenticated — deliberately outside
    # the try/except so it is never converted into a False/True.
    service = _get_youtube_service()
    response = service.videos().list(part="snippet", id=yt_video_id).execute()
    items = response.get("items", [])
    if not items:
        raise ValueError(
            f"YouTube video not found (videos.list returned nothing): {yt_video_id}"
        )
    snippet = dict(items[0].get("snippet") or {})
    if not str(snippet.get("title") or "").strip():
        raise ValueError(
            f"YouTube video {yt_video_id} has no title in its snippet; "
            "refusing to update the description without one."
        )
    snippet["description"] = description
    for attempt in range(1, 4):
        try:
            service.videos().update(
                part="snippet",
                body={"id": yt_video_id, "snippet": snippet},
            ).execute()
            return True
        except Exception as e:
            if _is_transient_youtube_error(e) and attempt < 3:
                delay = min(2 ** attempt, 10)
                logger.warning(
                    f"Description update failed transiently ({e}); retrying in "
                    f"{delay}s (attempt {attempt}/3)."
                )
                time.sleep(delay)
                continue
            logger.error(f"Failed to update description for video {yt_video_id}: {e}")
            return False
    return False


def delete_video(yt_video_id: str) -> bool:
    """Delete video from YouTube.

    Raises when unauthenticated. Returns False only for a real API
    failure. Transient failures are retried internally (3 attempts,
    exponential backoff) before giving up with False.
    """
    logger.info(f"Deleting YouTube video {yt_video_id}.")
    # Raises RuntimeError when unauthenticated — deliberately outside
    # the try/except so it is never converted into a False/True.
    service = _get_youtube_service()
    for attempt in range(1, 4):
        try:
            service.videos().delete(id=yt_video_id).execute()
            return True
        except Exception as e:
            if _is_transient_youtube_error(e) and attempt < 3:
                delay = min(2 ** attempt, 10)
                logger.warning(
                    f"Delete failed transiently ({e}); retrying in "
                    f"{delay}s (attempt {attempt}/3)."
                )
                time.sleep(delay)
                continue
            logger.error(f"Failed to delete video {yt_video_id}: {e}")
            return False
    return False
