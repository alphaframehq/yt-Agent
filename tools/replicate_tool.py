"""Replicate AI integration tool for TubePilot AI.

Provides FLUX Schnell image generation fallback (~$0.01/image) when Gemini Imagen 3
is rate-limited or unavailable.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional
import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from tools.secrets import get_secret

__all__ = ["generate_flux_image"]

CACHE_DIR = Path("cache/images")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _get_cache_path(prompt: str) -> Path:
    h = hashlib.md5(f"flux:{prompt}".encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{h}.png"


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
def generate_flux_image(prompt: str, aspect_ratio: str = "16:9") -> Optional[str]:
    """Generate image using Replicate black-forest-labs/flux-schnell."""
    cache_path = _get_cache_path(prompt)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        logger.info(f"Replicate FLUX cache hit: {cache_path}")
        return str(cache_path.resolve())

    token = get_secret("REPLICATE_API_TOKEN")
    if not token:
        logger.warning("REPLICATE_API_TOKEN not configured. Skipping Replicate.")
        return None

    try:
        import replicate

        logger.info(f"Calling Replicate FLUX for: {prompt[:60]}...")
        output = replicate.run(
            "black-forest-labs/flux-schnell",
            input={"prompt": prompt, "aspect_ratio": aspect_ratio, "output_format": "png"},
        )
        if output:
            img_url = output[0] if isinstance(output, list) else str(output)
            with httpx.Client(timeout=40.0) as client:
                resp = client.get(img_url)
                resp.raise_for_status()
                cache_path.write_bytes(resp.content)
                return str(cache_path.resolve())
    except Exception as e:
        logger.warning(f"Replicate FLUX call failed: {e}")
        return None

    return None
