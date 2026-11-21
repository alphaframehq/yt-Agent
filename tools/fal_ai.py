"""fal.ai video generation tool for TubePilot AI.

Generates AI video clips (e.g. LTX-Video) for cinematic scenes within budget constraints (~$0.05/clip).
Cached deterministically in ./cache/video_ai/.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from tools.secrets import get_secret

__all__ = ["generate_fal_video"]

CACHE_DIR = Path("cache/video_ai")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _get_cache_path(prompt: str) -> Path:
    h = hashlib.md5(prompt.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"fal_{h}.mp4"


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
def generate_fal_video(prompt: str, duration_sec: int = 5) -> Optional[str]:
    """Generate AI video clip using fal.ai LTX-Video."""
    cache_path = _get_cache_path(prompt)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        logger.info(f"fal.ai video cache hit: {cache_path}")
        return str(cache_path.resolve())

    fal_key = get_secret("FAL_KEY")
    if not fal_key:
        logger.warning("FAL_KEY not configured. Skipping fal.ai video generation.")
        return None

    try:
        import fal_client

        logger.info(f"Submitting fal.ai LTX-Video task for prompt: {prompt[:60]}...")
        handler = fal_client.submit(
            "fal-ai/ltx-video",
            arguments={"prompt": prompt, "num_frames": int(duration_sec * 24)},
        )
        result = handler.get()
        video_url = result.get("video", {}).get("url")
        if video_url:
            import httpx
            with httpx.Client(timeout=60.0) as client:
                r = client.get(video_url)
                r.raise_for_status()
                cache_path.write_bytes(r.content)
                return str(cache_path.resolve())
    except Exception as e:
        logger.warning(f"fal.ai video generation failed: {e}")
        return None

    return None
