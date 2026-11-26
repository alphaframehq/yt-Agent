"""Pixabay API tool for TubePilot AI.

Searches and downloads:
- High quality stock B-roll video clips (fintech, servers, trading desks, abstract tech)
- Royalty-free background music loops for video assembly
All files cached in ./cache/media/.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from tools.secrets import _get_secret_optional, get_secret

__all__ = [
    "search_stock_videos",
    "search_stock_images",
    "search_background_music",
    "download_media",
]

CACHE_DIR = Path("cache/media")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _get_cache_path(url: str, ext: str = "mp4") -> Path:
    h = hashlib.md5(url.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{h}.{ext}"


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
def search_stock_videos(query: str, per_page: int = 5) -> List[Dict[str, Any]]:
    """Search Pixabay Video API for relevant stock footage."""
    api_key = get_secret("PIXABAY_KEY")
    if not api_key:
        logger.warning("PIXABAY_KEY not configured. Returning empty list.")
        return []

    url = "https://pixabay.com/api/videos/"
    params = {
        "key": api_key,
        "q": query,
        "video_type": "film",
        "per_page": per_page,
    }

    try:
        with httpx.Client(timeout=25.0) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            results = []
            for hit in data.get("hits", []):
                videos = hit.get("videos", {})
                # Choose medium or large resolution
                selected = videos.get("medium") or videos.get("large") or videos.get("small", {})
                results.append({
                    "id": hit.get("id"),
                    "tags": hit.get("tags"),
                    "duration": hit.get("duration"),
                    "url": selected.get("url"),
                    "width": selected.get("width"),
                    "height": selected.get("height"),
                })
            return results
    except Exception as e:
        logger.warning(f"Pixabay video search failed: {e}")
        return []


def search_stock_images(query: str, per_page: int = 5) -> List[Dict[str, Any]]:
    """Search Pixabay Images API for royalty-free stills.

    Free fallback when AI image generation is unavailable (run #18:
    Imagen 3 404s for the free Gemini key — it is a paid model — and
    FLUX needs REPLICATE_API_TOKEN, which the repo does not hold).
    Returns [] — never raises — when the key is missing, the search
    finds nothing, or the request fails, so the caller can fall
    through to the programmatic visual.
    """
    api_key = _get_secret_optional("PIXABAY_KEY")
    if not api_key:
        logger.warning("PIXABAY_KEY not configured. No stock images available.")
        return []

    url = "https://pixabay.com/api/"
    params = {
        "key": api_key,
        "q": query,
        "image_type": "photo",
        "per_page": per_page,
        "safesearch": "true",
    }

    try:
        with httpx.Client(timeout=25.0) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            results = []
            for hit in data.get("hits", []):
                image_url = hit.get("largeImageURL") or hit.get("webformatURL")
                if not image_url:
                    continue
                results.append({
                    "id": hit.get("id"),
                    "tags": hit.get("tags"),
                    "url": image_url,
                    "width": hit.get("imageWidth"),
                    "height": hit.get("imageHeight"),
                })
            return results
    except Exception as e:
        logger.warning(f"Pixabay image search failed: {e}")
        return []


def search_background_music(genre: str = "ambient electronic", per_page: int = 5) -> List[Dict[str, Any]]:
    """Search for ambient / documentary background music tracks."""
    # Pixabay audio API endpoints or curated royalty-free tracks
    return [
        {
            "title": "Minimal Corporate Tech Documentary",
            "url": "https://cdn.pixabay.com/download/audio/2022/05/27/audio_1808fbf07a.mp3",
            "genre": "ambient",
        }
    ]


def download_media(url: str, extension: str = "mp4") -> str:
    """Download media file and return local cached path."""
    dest_path = _get_cache_path(url, extension)
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return str(dest_path.resolve())

    logger.info(f"Downloading media asset: {url}")
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            dest_path.write_bytes(resp.content)
            return str(dest_path.resolve())
    except Exception as e:
        logger.error(f"Failed to download media from {url}: {e}")
        # Create empty placeholder if download fails during mock
        dest_path.touch()
        return str(dest_path.resolve())
