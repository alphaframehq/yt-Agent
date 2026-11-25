# Added via PR feat/agnes-media (25 Nov 2026), on top of merged PR #1.
# See docs/GAPS.md "Next steps" and docs/SETUP.md §5.
# Source repo: alphaframehq/yt-Agent @ main c39f42541b8e784dcf41c09583dc57227be07667
#
# Agnes AI client for TubePilot AI.
#
# Verified 25 Nov 2026 by signed-in browser inspection of
# https://platform.agnes-ai.com/settings/apiKeys and the Agnes docs:
#   - OpenAI-compatible API, base https://apihub.agnes-ai.com/v1
#   - Auth: Authorization: Bearer <AGNES_API_KEY>
#   - Existing key named "mus" (created 2026-11-25, never used)
#   - Balance $0.00, no Token Plan, usage all zero
#   - Image: agnes-image-2.5-flash, POST /v1/images/generations,
#     1K/2K/4K, ratios 1:1, 2:3, 3:2, 3:4, 4:3, 9:16, 16:9, 21:9, PNG
#   - Video: agnes-video-2.5-flash, POST /v1/videos (async),
#     poll GET /agnesapi?video_id=<ID>&model_name=<model>, 5-10 sec,
#     720P/1080P/1K/2K
#   - Free access is an "Angel User Trial Price" of $0 for the Flash
#     models only. NO expiry/limit was stated on the pages seen —
#     re-check before relying on $0. Paid models need a top-up.
#
# NOT re-verified character-for-character in this session:
#   - Exact JSON field names for video-create `size` / `duration`.
#   - The poll endpoint root and its response fields.
#   - Whether the image endpoint returns b64_json or a URL.
# The code below handles the OpenAI-compatible variants for the image
# response, and parses the video poll response defensively (see
# _parse_poll_status). Verify all three against
# https://www.agnes-ai.com/en/docs/agnes-image-25-flash and
# https://www.agnes-ai.com/en/docs/agnes-video-25-flash before the
# first live call, and fix this file if the docs differ. Do not guess
# and ship.
#
# Fail-loud contract (same as the PR #1 patches): any missing key,
# HTTP error, payment-required / trial-ended response, empty payload,
# failed video task, or poll timeout RAISES. No placeholder image,
# no empty MP4, no fake video ID is ever returned as success.
# No secrets in this file — env-var / secrets-store names only.
"""Agnes AI image + video generation for TubePilot AI.

Models (free Angel Trial $0 — expiry/limit unverified, see header):
- agnes-image-2.5-flash : synchronous, POST /v1/images/generations
- agnes-video-2.5-flash : asynchronous, POST /v1/videos, then poll
  until completed / failed. 5-10 seconds, 720P / 1080P / 1K / 2K.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from loguru import logger

try:
    from tools.secrets import get_secret
except Exception:  # pragma: no cover - standalone / test import
    def get_secret(name: str) -> Optional[str]:  # type: ignore
        return os.environ.get(name) or None

__all__ = [
    "generate_image_agnes",
    "generate_video_clip",
    "AGNES_IMAGE_MODEL",
    "AGNES_VIDEO_MODEL",
    "DEFAULT_BASE_URL",
]

AGNES_IMAGE_MODEL = "agnes-image-2.5-flash"
AGNES_VIDEO_MODEL = "agnes-video-2.5-flash"
DEFAULT_BASE_URL = "https://apihub.agnes-ai.com/v1"

# Aspect ratio -> OpenAI-compatible size string (image endpoint).
# Agnes docs list sizes 1K / 2K / 4K and the ratios below; the exact
# "size" values are the OpenAI-compatible form. If the live API
# rejects a size, check the image docs and fix this map — do not
# silently fall back to another ratio.
_ASPECT_TO_SIZE = {
    "1:1": "1024x1024",
    "2:3": "1024x1536",
    "3:2": "1536x1024",
    "3:4": "1024x1365",
    "4:3": "1365x1024",
    "9:16": "1024x1792",
    "16:9": "1792x1024",
    "21:9": "2048x877",
}


def _base_url() -> str:
    """Agnes base URL, explicit env/secret first, documented default last."""
    url = get_secret("AGNES_BASE_URL") or os.environ.get("AGNES_BASE_URL")
    return (url or DEFAULT_BASE_URL).rstrip("/")


def _api_key() -> str:
    """Bearer key. Raises if missing — never proceed without auth."""
    key = get_secret("AGNES_API_KEY") or os.environ.get("AGNES_API_KEY")
    if not key:
        raise ValueError(
            "AGNES_API_KEY not set: store the Agnes key (named 'mus' in "
            "the Agnes account) as a Codespaces secret / in the secrets "
            "store. See setup-guide/SETUP.md §5c. No image/video call "
            "is attempted without it."
        )
    return key


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}


def _raise_for_trial_or_payment(resp: httpx.Response, what: str) -> None:
    """Raise a clear error for payment / trial-ended responses.

    A 402 / 403 with a billing message most likely means the Angel
    Trial $0 price no longer applies (balance is $0.00, no Token Plan).
    That is a billing fact, not a transient error — retrying will not
    help, and the caller must not continue as if the media existed.
    """
    if resp.status_code in (402, 403):
        body = (resp.text or "")[:500]
        raise RuntimeError(
            f"Agnes {what} refused with HTTP {resp.status_code} "
            f"(possible cause: Angel Trial $0 price ended / payment "
            f"required — account balance is $0.00, no Token Plan). "
            f"Re-check https://platform.agnes-ai.com/billing/balance "
            f"before retrying. Response: {body}"
        )
    resp.raise_for_status()


def _validate_file(path: Path, what: str, magic: bytes) -> Path:
    """A generated file must exist, be non-empty, and start with the
    expected magic bytes. A 0-byte / wrong-type file is never success."""
    if not path.exists():
        raise RuntimeError(f"Agnes {what} produced no file at {path}")
    data = path.read_bytes()
    if len(data) < 1024:
        raise RuntimeError(
            f"Agnes {what} produced a suspiciously small file "
            f"({len(data)} bytes) at {path}; refusing it."
        )
    if magic and not data.startswith(magic):
        raise RuntimeError(
            f"Agnes {what} file at {path} does not start with the "
            f"expected format header; refusing it."
        )
    return path


def generate_image_agnes(
    prompt: str,
    dest_path: str | Path,
    aspect_ratio: str = "16:9",
    size_label: str = "1K",
) -> Path:
    """Generate one image with agnes-image-2.5-flash (synchronous).

    Args:
        prompt: Text prompt (callers should pass the already-optimised
            prompt; this function does not invent prompt content).
        dest_path: Where to write the PNG.
        aspect_ratio: One of 1:1, 2:3, 3:2, 3:4, 4:3, 9:16, 16:9, 21:9.
        size_label: "1K" / "2K" / "4K" — recorded in the log only;
            the request uses the OpenAI-compatible `size` field.

    Returns:
        Path to the written PNG.

    Raises:
        ValueError: missing AGNES_API_KEY / unknown aspect ratio.
        RuntimeError / httpx.HTTPError: any API failure, empty payload,
            or invalid file. Never returns a placeholder.
    """
    if aspect_ratio not in _ASPECT_TO_SIZE:
        raise ValueError(
            f"Unknown aspect ratio '{aspect_ratio}': expected one of "
            f"{sorted(_ASPECT_TO_SIZE)}."
        )
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"{_base_url()}/images/generations"
    payload = {
        "model": AGNES_IMAGE_MODEL,
        "prompt": prompt,
        "size": _ASPECT_TO_SIZE[aspect_ratio],
    }
    logger.info(
        f"Agnes image: model={AGNES_IMAGE_MODEL} size={size_label} "
        f"aspect={aspect_ratio} prompt={prompt[:60]}..."
    )
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(url, headers=_headers(), json=payload)
        _raise_for_trial_or_payment(resp, "image generation")
        data = resp.json()

    # OpenAI-compatible response: data[0].b64_json or data[0].url.
    items = data.get("data") if isinstance(data, dict) else None
    if not items:
        raise RuntimeError(
            f"Agnes image generation returned no image data "
            f"(response keys: {sorted(data) if isinstance(data, dict) else type(data)}). "
            "Check the image docs — the response shape may differ."
        )
    first = items[0] or {}
    if first.get("b64_json"):
        import base64

        img_bytes = base64.b64decode(first["b64_json"])
    elif first.get("url"):
        with httpx.Client(timeout=120.0) as client:
            dl = client.get(first["url"])
            dl.raise_for_status()
            img_bytes = dl.content
    else:
        raise RuntimeError(
            f"Agnes image response has neither b64_json nor url "
            f"(keys: {sorted(first)}). Check the image docs."
        )
    if not img_bytes:
        raise RuntimeError("Agnes image generation returned empty image bytes.")
    dest.write_bytes(img_bytes)
    return _validate_file(dest, "image", magic=b"\x89PNG")


def _poll_base_url() -> str:
    """Root for the video poll endpoint.

    Docs report the poll as GET /agnesapi?video_id=... — i.e. off the
    API root, not under /v1. Strip a trailing /v1 from the base URL.
    If the live API 404s here, check the video docs and fix this.
    """
    base = _base_url()
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base.rstrip("/")


def _parse_poll_status(payload: Any) -> tuple[str, Optional[str]]:
    """Return (status, video_url) from a poll response, defensively.

    Status values seen in docs: completed / failed (plus in-progress
    states). The video URL field name was not verified — accept the
    common variants and raise on an unrecognised shape instead of
    guessing that a task succeeded.
    """
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Agnes video poll returned an unexpected shape "
            f"({type(payload)}); cannot determine task status."
        )
    # Unwrap a nested "data" object if present.
    obj: Dict[str, Any] = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    status = str(
        obj.get("status") or obj.get("state") or obj.get("task_status") or ""
    ).lower()
    url = (
        obj.get("video_url")
        or obj.get("url")
        or obj.get("download_url")
        or obj.get("videoUrl")
        or None
    )
    if isinstance(url, dict):
        url = url.get("url")
    if not status:
        raise RuntimeError(
            f"Agnes video poll response has no status field "
            f"(keys: {sorted(obj)}). Check the video docs."
        )
    return status, (str(url) if url else None)


def generate_video_clip(
    prompt: str,
    dest_path: str | Path,
    duration_sec: int = 5,
    size: str = "720P",
    poll_timeout_sec: Optional[int] = None,
    poll_interval_sec: Optional[int] = None,
    image_url: Optional[str] = None,
) -> Path:
    """Generate a 5-10 s clip with agnes-video-2.5-flash (async).

    Flow: POST /v1/videos (create task) -> poll
    GET /agnesapi?video_id=<ID>&model_name=agnes-video-2.5-flash
    until completed / failed -> download the video URL.

    Args:
        prompt: Text prompt for the clip.
        dest_path: Where to write the MP4.
        duration_sec: 5-10 (Agnes Video 2.5 Flash range). Out-of-range
            raises — the API must not be asked for a length it does
            not support.
        size: "720P" / "1080P" / "1K" / "2K".
        poll_timeout_sec / poll_interval_sec: defaults from env
            AGNES_VIDEO_POLL_TIMEOUT_SEC (600) /
            AGNES_VIDEO_POLL_INTERVAL_SEC (10).
        image_url: optional first-frame / reference image URL.

    Returns:
        Path to the written MP4.

    Raises:
        ValueError: missing key, bad duration/size.
        RuntimeError: create failure, task failed, poll timeout, empty
            download, or unrecognised poll shape. TimeoutError is
            raised as RuntimeError with the task ID so the caller can
            check the Agnes dashboard manually. Never returns a
            placeholder or a fake video ID.
    """
    if not 5 <= duration_sec <= 10:
        raise ValueError(
            f"Agnes Video 2.5 Flash supports 5-10 seconds, got "
            f"{duration_sec}. Use a different model for longer clips."
        )
    if size not in {"720P", "1080P", "1K", "2K"}:
        raise ValueError(f"Unknown Agnes video size '{size}': expected 720P/1080P/1K/2K.")
    timeout = poll_timeout_sec or int(
        os.environ.get("AGNES_VIDEO_POLL_TIMEOUT_SEC", "600")
    )
    interval = poll_interval_sec or int(
        os.environ.get("AGNES_VIDEO_POLL_INTERVAL_SEC", "10")
    )
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    create_url = f"{_base_url()}/videos"
    payload: Dict[str, Any] = {
        "model": AGNES_VIDEO_MODEL,
        "prompt": prompt,
        "size": size,
        "duration": duration_sec,
    }
    if image_url:
        payload["image_url"] = image_url
    logger.info(
        f"Agnes video: model={AGNES_VIDEO_MODEL} size={size} "
        f"duration={duration_sec}s prompt={prompt[:60]}..."
    )
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(create_url, headers=_headers(), json=payload)
        _raise_for_trial_or_payment(resp, "video creation")
        created = resp.json()

    # Task/video ID field name was not verified — accept common variants.
    video_id = None
    if isinstance(created, dict):
        video_id = (
            created.get("video_id")
            or created.get("id")
            or created.get("task_id")
            or (created.get("data") or {}).get("video_id")
            or (created.get("data") or {}).get("id")
        )
    if not video_id:
        raise RuntimeError(
            f"Agnes video creation returned no video/task ID "
            f"(response keys: {sorted(created) if isinstance(created, dict) else type(created)}). "
            "Check the video docs — the create response shape may differ."
        )
    logger.info(f"Agnes video task created: video_id={video_id}; polling...")

    poll_url = f"{_poll_base_url()}/agnesapi"
    deadline = time.monotonic() + timeout
    last_status = "unknown"
    with httpx.Client(timeout=60.0) as client:
        while True:
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"Agnes video task {video_id} did not complete "
                    f"within {timeout}s (last status: {last_status}). "
                    "Check the task in the Agnes dashboard; do not "
                    "treat the clip as generated."
                )
            resp = client.get(
                poll_url,
                headers=_headers(),
                params={"video_id": video_id, "model_name": AGNES_VIDEO_MODEL},
            )
            _raise_for_trial_or_payment(resp, "video poll")
            status, video_url = _parse_poll_status(resp.json())
            last_status = status
            if status in {"completed", "success", "succeeded"}:
                if not video_url:
                    raise RuntimeError(
                        f"Agnes video task {video_id} reports completed "
                        "but returned no video URL."
                    )
                dl = client.get(video_url)
                dl.raise_for_status()
                if not dl.content:
                    raise RuntimeError(
                        f"Agnes video task {video_id} download was empty."
                    )
                dest.write_bytes(dl.content)
                logger.info(f"Agnes video completed: {video_id} -> {dest}")
                return _validate_file(dest, "video clip", magic=b"")
            if status in {"failed", "error", "cancelled"}:
                raise RuntimeError(
                    f"Agnes video task {video_id} failed "
                    f"(status: {status}). No clip was produced."
                )
            # In progress (queued / processing / running / ...): wait.
            time.sleep(interval)
