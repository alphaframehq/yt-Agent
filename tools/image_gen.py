# Applied via PR #1 (fix/production-mocks), 2026-11-25; Agnes wiring
# added via PR feat/agnes-media (25 Nov 2026).
# This file = the PR #1-patched tools/image_gen.py PLUS Agnes wiring
# (the only changes vs that version are marked "AGNES WIRING").
# Source: alphaframehq/yt-Agent @ main c39f42541b8e784dcf41c09583dc57227be07667
# Fixes audit P0 #9/#10: generate_image() now RAISES the original error on
# any Imagen/Gemini/FLUX failure instead of returning a Pillow branded card
# or a 0-byte file. _generate_gemini_native() no longer unconditionally
# returns the fallback stub — the original code contained no real Gemini
# native image API call, so it raises NotImplementedError (see below).
# Placeholders exist only behind an explicit ALLOW_DEMO_MODE env flag
# (default off), and a failed placeholder never touches a 0-byte file.
# AGNES WIRING (25 Nov 2026): generate_image() can now use Agnes AI
# (agnes-image-2.5-flash, free Angel Trial $0 — expiry/limit unverified)
# when the caller passes model="agnes/..." OR when AGNES_API_KEY is set
# and AGNES_AS_DEFAULT_IMAGE_PROVIDER=1. Agnes failures raise exactly
# like Imagen/FLUX failures. No Pillow placeholder on the success path.
# See tools/agnes_ai.py (new file, same patch set) for the client.
"""Image generation module for TubePilot AI.

Supports:
- Gemini Imagen 3 (imagen-3.0-generate-001)
- Gemini 2.0 Flash native image generation
- Replicate FLUX Schnell fallback

Features:
- MD5 caching in ./cache/images/ to save costs and eliminate redundant generations
- Dark mode, infographic-style prompting optimization to prevent safety blocks
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Optional
import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from tools.secrets import (
    _get_secret_optional,
    get_gemini_pool_keys,
    mark_gemini_key_failed,
    mask_secret,
)

__all__ = [
    "generate_image",
    "generate_programmatic_visual",
    "get_cached_image_path",
]

CACHE_DIR = Path("cache/images")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _demo_mode_enabled() -> bool:
    """True only when the caller explicitly opts into demo placeholders.

    Default off. Production must never set ALLOW_DEMO_MODE.
    """
    return os.environ.get("ALLOW_DEMO_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def get_cached_image_path(prompt: str, model: str) -> Path:
    """Compute deterministic cache path using MD5 hash of prompt and model."""
    key = f"{model}:{prompt}".encode("utf-8")
    hash_str = hashlib.md5(key).hexdigest()
    return CACHE_DIR / f"{hash_str}.png"


def sanitize_finance_prompt(prompt: str) -> str:
    """Enhance prompt to adhere to clean fintech aesthetic and avoid safety blocks."""
    suffix = ", clean modern infographic style, dark background #0E1117, high contrast neon cyan and gold accents, 4k resolution, cinematic lighting, no human faces, no text"
    return prompt.strip() + suffix


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
def generate_image(
    prompt: str,
    model: str = "gemini/imagen-3.0-generate-001",
    aspect_ratio: str = "16:9",
) -> str:
    """Generate image with aggressive caching, fallback chain, and budget awareness.

    Returns:
        Absolute or relative path to the generated PNG image.

    Raises:
        The original provider error if image generation fails. No Pillow
        placeholder card and no 0-byte file are ever returned as success
        in production (placeholders only behind ALLOW_DEMO_MODE).
    """
    optimized_prompt = sanitize_finance_prompt(prompt)
    cache_path = get_cached_image_path(optimized_prompt, model)

    # 1. Check cache
    if cache_path.exists() and cache_path.stat().st_size > 0:
        logger.info(f"Image cache hit: {cache_path}")
        return str(cache_path.resolve())

    logger.info(f"Generating image with model '{model}' for prompt: {prompt[:60]}...")

    # AGNES WIRING: explicit "agnes/..." model, or Agnes as the
    # configured default (AGNES_API_KEY set + AGNES_AS_DEFAULT_IMAGE_PROVIDER=1).
    # Otherwise fall through to the existing Imagen / Gemini / FLUX paths.
    if "agnes" in model or _agnes_as_default():
        img_path = _generate_agnes(optimized_prompt, cache_path, aspect_ratio)
        return str(img_path.resolve())

    # 2. Try primary: Imagen 3 / Gemini / FLUX. Any failure propagates
    # as the original error — there is no silent placeholder fallback.
    if "imagen-3" in model:
        img_path = _generate_imagen3(optimized_prompt, cache_path, aspect_ratio)
        return str(img_path.resolve())
    elif "gemini" in model:
        img_path = _generate_gemini_native(optimized_prompt, cache_path)
        return str(img_path.resolve())
    elif "flux" in model or "replicate" in model:
        img_path = _generate_flux(optimized_prompt, cache_path, aspect_ratio)
        return str(img_path.resolve())
    else:
        raise ValueError(f"Unknown image model '{model}': no provider implemented for it.")

def _agnes_as_default() -> bool:
    """True only when the operator explicitly made Agnes the default
    image provider AND a key is configured. Default off — without this
    flag, Agnes is used only for an explicit model="agnes/..." call,
    so existing Imagen/FLUX callers are unaffected."""
    if os.environ.get("AGNES_AS_DEFAULT_IMAGE_PROVIDER", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return False
    try:
        from tools.secrets import get_secret as _get_secret

        return bool(_get_secret("AGNES_API_KEY") or os.environ.get("AGNES_API_KEY"))
    except Exception:
        return bool(os.environ.get("AGNES_API_KEY"))


def _generate_agnes(prompt: str, dest_path: Path, aspect_ratio: str = "16:9") -> Path:
    """Call Agnes AI agnes-image-2.5-flash (see tools/agnes_ai.py).

    Raises the original Agnes error on any failure — no Pillow
    placeholder, no 0-byte file. A missing AGNES_API_KEY raises
    ValueError from the client, which is the correct fail-loud
    behaviour (do not fall through to Imagen here: the caller
    explicitly asked for Agnes).
    """
    from tools.agnes_ai import generate_image_agnes

    return generate_image_agnes(prompt, dest_path, aspect_ratio=aspect_ratio)


def _generate_imagen3(prompt: str, dest_path: Path, aspect_ratio: str = "16:9") -> Path:
    """Call Google Gemini Imagen 3 API with key pool failover."""
    keys = get_gemini_pool_keys()
    if not keys:
        raise ValueError("GEMINI_KEY not set in pool")

    payload = {
        "instances": [{"prompt": prompt}],
        "parameters": {
            "sampleCount": 1,
            "aspectRatio": aspect_ratio,
            "outputMimeType": "image/png",
        },
    }

    last_err: Optional[Exception] = None
    for idx, api_key in enumerate(keys):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/imagen-3.0-generate-001:predict?key={api_key}"
        try:
            with httpx.Client(timeout=45.0) as client:
                resp = client.post(url, json=payload)
                if resp.status_code == 429:
                    logger.warning(
                        f"Imagen 3 key [{mask_secret(api_key)}] hit 429 Rate Limit. "
                        f"Rotating to next key in pool ({idx + 1}/{len(keys)})..."
                    )
                    mark_gemini_key_failed(api_key, error_status=429, cooldown_sec=60.0)
                    last_err = httpx.HTTPStatusError("429 Rate Limit", request=resp.request, response=resp)
                    continue
                elif resp.status_code in (401, 403):
                    logger.warning(
                        f"Imagen 3 key [{mask_secret(api_key)}] failed with HTTP {resp.status_code}. "
                        f"Rotating to next key in pool ({idx + 1}/{len(keys)})..."
                    )
                    mark_gemini_key_failed(api_key, error_status=resp.status_code, cooldown_sec=3600.0)
                    last_err = httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                    continue
                elif resp.status_code == 404:
                    # Run #18: the model endpoint itself is unavailable
                    # for this account — Imagen 3 is a PAID model and
                    # the free Gemini key 404s here. Rotating keys
                    # cannot help (every free key hits the same
                    # missing model) and marking the key failed would
                    # poison the text-generation pool for a problem
                    # that is not the key's fault. Fail fast with a
                    # clear error; the caller (agents/visuals.py)
                    # falls back to free stock imagery instead.
                    logger.error(
                        "Imagen 3 request failed: HTTP 404 — the model "
                        "is not served for this Gemini API key "
                        "(Imagen 3 is a paid model). Not rotating keys."
                    )
                    raise RuntimeError(
                        "Imagen 3 unavailable (HTTP 404): the model is "
                        "not served for this Gemini API key (Imagen 3 is "
                        "a paid model). Use the Pixabay / programmatic "
                        "fallback instead."
                    )

                resp.raise_for_status()
                data = resp.json()
                predictions = data.get("predictions", [])
                if not predictions:
                    raise RuntimeError("No predictions returned from Imagen 3")

                b64_img = predictions[0].get("bytesBase64Encoded")
                img_bytes = base64.b64decode(b64_img)
                if not img_bytes:
                    raise RuntimeError("Imagen 3 returned empty image bytes.")
                dest_path.write_bytes(img_bytes)
                return dest_path
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.warning(f"Imagen 3 request failed with key [{mask_secret(api_key)}]: {exc}")
            mark_gemini_key_failed(api_key, error_status=getattr(getattr(exc, 'response', None), 'status_code', 500))
            last_err = exc
            continue

    if last_err is not None:
        raise last_err
    raise RuntimeError("All Gemini API keys in pool failed for Imagen 3.")


def _generate_gemini_native(prompt: str, dest_path: Path) -> Path:
    """Call Gemini native image generation.

    NOT IMPLEMENTED: the original code never made a Gemini native image
    API call — it unconditionally returned the Pillow fallback stub, even
    with a valid Gemini key. No model name, endpoint, request shape or
    response parsing for the native image path exists in the original
    code, so a real call cannot be reconstructed from it. Implement it
    against the documented Gemini native image generation API (e.g. a
    gemini-3.8-flash image model via generateContent with image output)
    and replace this raise; do not silently return a placeholder.
    """
    raise NotImplementedError(
        "Gemini native image generation is not implemented: the original "
        "code contained no real API call for this path (it always returned "
        "the Pillow fallback graphic). Implement the real Gemini native "
        "image call, or use model 'gemini/imagen-3.0-generate-001' / a "
        "FLUX model instead."
    )

def _generate_flux(prompt: str, dest_path: Path, aspect_ratio: str = "16:9") -> Path:
    """Call Replicate FLUX Schnell."""
    token = _get_secret_optional("REPLICATE_API_TOKEN")
    if not token:
        raise ValueError("REPLICATE_API_TOKEN not set")

    import replicate
    output = replicate.run(
        "black-forest-labs/flux-schnell",
        input={"prompt": prompt, "aspect_ratio": aspect_ratio, "output_format": "png"},
    )
    if output and len(output) > 0:
        url = output[0] if isinstance(output, list) else str(output)
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            if not resp.content:
                raise RuntimeError("Replicate FLUX returned empty image bytes.")
            dest_path.write_bytes(resp.content)
            return dest_path
    raise RuntimeError("No output from Replicate FLUX")


def generate_programmatic_visual(
    prompt: str, dest_path: Path, aspect_ratio: str = "16:9"
) -> Path:
    """Last-resort production visual: a dark branded title card
    rendered locally with Pillow (run #18 fix).

    This is NOT a generated image and must never be presented as
    one: callers log a warning naming the fallback whenever they
    use it. It exists so a single image-provider failure can never
    kill finalize — when Imagen 404s (paid model, free key) AND no
    free stock image is available, the scene still gets a real,
    renderable visual (the same house style as the diagram /
    programmatic visuals the pipeline already renders for $0.00).
    Distinct from the demo-only ``_generate_fallback_graphic``:
    that one is gated behind ALLOW_DEMO_MODE and stands in for a
    whole failed generation; this one is an explicit, logged
    production degradation path. Always writes a real non-empty
    PNG; raises on any Pillow failure.
    """
    from PIL import Image, ImageDraw

    width, height = (720, 1280) if aspect_ratio == "9:16" else (1280, 720)
    img = Image.new("RGB", (width, height), color=(14, 17, 23))
    draw = ImageDraw.Draw(img)

    step = 80
    for y in range(0, height, step):
        draw.line([(0, y), (width, y)], fill=(30, 36, 48), width=1)
    for x in range(0, width, step):
        draw.line([(x, 0), (x, height)], fill=(30, 36, 48), width=1)
    margin = 60
    draw.rectangle(
        [margin, margin, width - margin, height - margin],
        outline=(0, 212, 170),
        width=3,
    )
    draw.rectangle(
        [margin + 4, margin + 4, width - margin - 4, height - margin - 4],
        outline=(255, 107, 107),
        width=1,
    )

    # Title lines from the prompt's opening words, wrapped.
    words = str(prompt or "").split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        current.append(word)
        if len(" ".join(current)) > 32:
            lines.append(" ".join(current))
            current = []
    if current:
        lines.append(" ".join(current))
    y = height // 3
    for line in lines[:5]:
        draw.text((margin + 30, y), line, fill=(230, 237, 243))
        y += 44
    draw.text((margin + 30, height - margin - 40), "TUBEPILOT AI", fill=(0, 212, 170))

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest_path, "PNG")
    return dest_path


def _generate_fallback_graphic(prompt: str, dest_path: Path) -> str:
    """Demo-only: generate a branded Pillow placeholder card.

    Raises unless ALLOW_DEMO_MODE is explicitly enabled. Production
    callers must never use this as a success path — generate_image()
    raises the original provider error instead. If Pillow fails, the
    original error is raised; a 0-byte file is never created via touch().
    """
    if not _demo_mode_enabled():
        raise RuntimeError(
            "Placeholder graphics are disabled: set ALLOW_DEMO_MODE=1 "
            "explicitly for local dev only. Production generate_image() "
            "must raise the original provider error instead of returning "
            "a Pillow fallback card."
        )
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1280, 720), color=(14, 17, 23))
    draw = ImageDraw.Draw(img)

    # Draw tech-finance decorative grid lines
    for y in range(0, 720, 80):
        draw.line([(0, y), (1280, y)], fill=(30, 36, 48), width=1)
    for x in range(0, 1280, 80):
        draw.line([(x, 0), (x, 720)], fill=(30, 36, 48), width=1)

    # Draw glowing accent rect
    draw.rectangle([100, 100, 1180, 620], outline=(0, 212, 170), width=3)
    draw.rectangle([104, 104, 1176, 616], outline=(255, 107, 107), width=1)

    # Header
    draw.text((140, 140), "TUBEPILOT AI // FINTECH ARCHITECTURE", fill=(0, 212, 170))

    # Main prompt summary
    words = prompt.split()
    lines = []
    current = []
    for w in words:
        current.append(w)
        if len(" ".join(current)) > 45:
            lines.append(" ".join(current))
            current = []
    if current:
        lines.append(" ".join(current))

    y_offset = 240
    for line in lines[:4]:
        draw.text((140, y_offset), line, fill=(230, 237, 243))
        y_offset += 50

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest_path, "PNG")
    logger.warning(f"DEMO MODE: wrote placeholder graphic to {dest_path} (not a real generated image).")
    return str(dest_path.resolve())
