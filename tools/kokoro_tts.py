# Kokoro TTS tool for TubePilot AI — local ONNX fallback (27 Nov 2026).
#
# DECISION (27 Nov 2026, run #22): Gemini TTS is still the PRIMARY
# provider, but it is a free-tier API — runs #19/#20 died on HTTP 429
# with one key, and run #22 died the same way with BOTH keys (2/2)
# at scene 11 of 14. Kokoro is the fallback that breaks that cycle:
# it runs entirely on the runner's CPU (ONNX Runtime, no API key, no
# network quota, no per-minute limit), so a scene can always be
# synthesized even when every Gemini key is cooling down.
#
# Model: Kokoro-82M (Apache 2.0) via the `kokoro-onnx` package
# (MIT). The ONNX model (~310 MB) + voices file download once from
# the kokoro-onnx GitHub release into cache/kokoro/ on first use
# (internet is on in the runner); they are re-downloaded on every
# fresh runner because the workspace is ephemeral — no workflow
# change is needed, requirements.txt already flows through the
# existing `pip install -r requirements.txt` step.
#
# Voice: the channel's Gemini voice is Kore (female). The Kokoro
# fallback voice is af_heart (female, the standard quality voice).
# The voice is deliberately NOT identical to Kore — a fallback scene
# may sound slightly different from its neighbours; that is accepted
# over losing the whole video to a 429. Voice mapping mirrors
# tools/gemini_tts.py (Joanna/Kore/Aoede -> af_heart,
# Matthew/Charon -> am_adam).
#
# Output format: identical contract to tools/gemini_tts.py —
# mono 16-bit WAV at 24 kHz, written with stdlib `wave`, duration
# measured from the real frames. Cache files are .wav under
# cache/audio/ keyed with a "kokoro:" prefix so they can never
# collide with Gemini's cache entries. agents/assemble.py needs no
# change (it already reads .wav via moviepy AudioFileClip).
#
# Fail-loud contract (mirrors tools/gemini_tts.py): a missing
# kokoro-onnx install, a failed model download, empty text, or
# empty audio raises RuntimeError — never silent/stub audio.
"""Kokoro TTS (local ONNX, no API key) — fallback for TubePilot AI.

Used only when every Gemini TTS key has failed (see agents/tts.py).
Primary remains Gemini TTS (tools/gemini_tts.py).
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
import wave
from pathlib import Path
from typing import Optional, Tuple

from loguru import logger

__all__ = [
    "synthesize_speech",
    "resolve_voice",
    "DEFAULT_VOICE",
    "MODEL_DIR",
]

# Kokoro-82M v1.0 model files, as documented by kokoro-onnx
# (github.com/thewh1teagle/kokoro-onnx, README "Setup").
_MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases"
    "/download/model-files-v1.1/kokoro-v1.0.onnx"
)
_VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases"
    "/download/model-files-v1.1/voices-v1.0.bin"
)
_MODEL_FILENAME = "kokoro-v1.0.onnx"
_VOICES_FILENAME = "voices-v1.0.bin"
# The v1.0 ONNX model is ~310 MB; anything far smaller is a
# truncated/failed download, not a model. The voices file is
# ~50 MB. Both bounds are sanity checks only — the Kokoro
# constructor is the real validator.
_MIN_MODEL_BYTES = 50_000_000
_MIN_VOICES_BYTES = 1_000_000

DEFAULT_VOICE = "af_heart"
_DEFAULT_LANG = "en-us"
_DEFAULT_SPEED = 1.0
_DEFAULT_SAMPLE_RATE_HZ = 24_000
# Same input guard as Gemini TTS: scenes are short narrations.
_MAX_TEXT_CHARS = 8_000
_DOWNLOAD_ATTEMPTS = 3

# Model files live under the workspace cache (ephemeral on the
# runner; re-downloaded on first use each run). Env override for
# local dev / tests: KOKORO_MODEL_DIR.
MODEL_DIR = Path(os.environ.get("KOKORO_MODEL_DIR", "cache/kokoro"))
CACHE_DIR = Path("cache/audio")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# A Kokoro voice name passed directly is used as-is when it looks
# like a Kokoro voice id (af_/am_/bf_/bm_ prefix); anything else
# maps through the channel's voice names, defaulting to af_heart.
_VOICE_MAP = {
    "Joanna": "af_heart",
    "Kore": "af_heart",
    "Aoede": "af_heart",
    "Kajal": "af_heart",
    "Aditi": "af_heart",
    "Raveena": "af_heart",
    "Matthew": "am_adam",
    "Charon": "am_adam",
}
_KOKORO_VOICE_PREFIXES = ("af_", "am_", "bf_", "bm_")

_kokoro_instance = None  # module-level cache of the Kokoro object


def resolve_voice(voice: Optional[str]) -> str:
    """Map a configured voice name to a Kokoro voice id."""
    override = os.environ.get("KOKORO_TTS_VOICE", "").strip()
    if override:
        return override
    if not voice:
        return DEFAULT_VOICE
    name = str(voice).strip()
    if name.startswith(_KOKORO_VOICE_PREFIXES):
        return name
    return _VOICE_MAP.get(name, DEFAULT_VOICE)


def _get_cache_path(text: str, voice: str) -> Path:
    h = hashlib.md5(f"kokoro:{voice}:{text}".encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{h}.wav"


def _measure_wav_duration(path: Path) -> float:
    """Return the exact duration of the WAV at *path* from its frames.

    Raises if the file is not a readable WAV with a positive length —
    a word-count estimate is never returned as a real duration.
    """
    try:
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            if rate <= 0 or frames <= 0:
                raise RuntimeError(
                    f"WAV {path} has no usable audio frames "
                    f"(frames={frames}, rate={rate})."
                )
            return round(frames / rate, 2)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(
            f"Could not measure real audio duration of {path}: {e}."
        ) from e


def _download(url: str, dest: Path, min_bytes: int) -> None:
    """Download *url* to *dest* (up to 3 attempts), verifying size.

    Raises RuntimeError on every failure — a missing model must fail
    the scene loudly, never produce silence.
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        try:
            if dest.exists():
                dest.unlink()
            urllib.request.urlretrieve(url, str(dest))
            size = dest.stat().st_size
            if size < min_bytes:
                raise RuntimeError(
                    f"Downloaded {dest.name} is only {size} bytes "
                    f"(expected at least {min_bytes}) — truncated download."
                )
            logger.info(f"Kokoro model file downloaded: {dest} ({size} bytes).")
            return
        except Exception as e:  # noqa: BLE001 - retried, then raised below
            last_err = e
            logger.warning(
                f"Kokoro model download attempt {attempt}/"
                f"{_DOWNLOAD_ATTEMPTS} failed for {url}: {e}"
            )
    raise RuntimeError(
        f"Could not download Kokoro model file from {url} after "
        f"{_DOWNLOAD_ATTEMPTS} attempts: {last_err}. No Kokoro "
        "fallback audio can be synthesized."
    ) from last_err


def _ensure_model_files() -> Tuple[Path, Path]:
    """Return (model_path, voices_path), downloading them if absent."""
    model_dir = Path(os.environ.get("KOKORO_MODEL_DIR", str(MODEL_DIR)))
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / _MODEL_FILENAME
    voices_path = model_dir / _VOICES_FILENAME
    if not model_path.exists() or model_path.stat().st_size < _MIN_MODEL_BYTES:
        logger.info(f"Kokoro model not cached; downloading {_MODEL_FILENAME} ...")
        _download(_MODEL_URL, model_path, _MIN_MODEL_BYTES)
    if not voices_path.exists() or voices_path.stat().st_size < _MIN_VOICES_BYTES:
        logger.info(f"Kokoro voices not cached; downloading {_VOICES_FILENAME} ...")
        _download(_VOICES_URL, voices_path, _MIN_VOICES_BYTES)
    return model_path, voices_path


def _get_kokoro():
    """Build (once) and return the Kokoro ONNX engine.

    The kokoro_onnx import is lazy so importing this module never
    fails when the package is absent — synthesize_speech() raises a
    clear error instead. Raises RuntimeError if the package is not
    installed or the model files cannot load.
    """
    global _kokoro_instance
    if _kokoro_instance is not None:
        return _kokoro_instance
    try:
        from kokoro_onnx import Kokoro
    except ImportError as e:
        raise RuntimeError(
            "Kokoro TTS fallback needs the 'kokoro-onnx' package "
            "(requirements.txt: kokoro-onnx==0.6.1), which is not "
            f"installed: {e}"
        ) from e
    model_path, voices_path = _ensure_model_files()
    try:
        _kokoro_instance = Kokoro(str(model_path), str(voices_path))
    except Exception as e:
        raise RuntimeError(
            f"Could not load Kokoro model from {model_path}: {e}"
        ) from e
    return _kokoro_instance


def _speed() -> float:
    try:
        return float(os.environ.get("KOKORO_TTS_SPEED", str(_DEFAULT_SPEED)).strip())
    except (TypeError, ValueError):
        return _DEFAULT_SPEED


def synthesize_speech(
    text: str,
    voice: str = "Joanna",
    engine: str = "neural",
    model: Optional[str] = None,
) -> Tuple[str, float]:
    """Synthesize speech locally with Kokoro and return (audio_path, duration_seconds).

    Same signature and return contract as tools/gemini_tts.synthesize_speech
    so agents/tts.py can call either engine interchangeably. ``engine``
    and ``model`` are accepted for drop-in compatibility and ignored —
    Kokoro has no engine switch and a single model file.

    Raises on any failure (package missing, model download/load
    failure, empty text, empty audio). Never returns silent or stub
    audio as success. The returned duration is measured from the
    real WAV frames. A cached file is re-measured, never trusted
    from a stored estimate.
    """
    if not text or not str(text).strip():
        raise ValueError("Refusing Kokoro TTS: text is empty.")
    clean_text = str(text).strip()
    if len(clean_text) > _MAX_TEXT_CHARS:
        raise ValueError(
            f"Refusing Kokoro TTS: text is {len(clean_text)} chars "
            f"(limit {_MAX_TEXT_CHARS}); split the narration first."
        )
    kokoro_voice = resolve_voice(voice)

    cache_path = _get_cache_path(clean_text, kokoro_voice)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return str(cache_path.resolve()), _measure_wav_duration(cache_path)

    kokoro = _get_kokoro()
    try:
        samples, sample_rate = kokoro.create(
            clean_text, voice=kokoro_voice, speed=_speed(), lang=_DEFAULT_LANG
        )
    except Exception as e:
        raise RuntimeError(f"Kokoro TTS synthesis failed: {e}") from e
    if samples is None or len(samples) == 0:
        raise RuntimeError("Kokoro TTS returned no audio samples.")
    if not sample_rate or int(sample_rate) <= 0:
        sample_rate = _DEFAULT_SAMPLE_RATE_HZ

    try:
        import numpy as np

        pcm = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
        pcm16 = (pcm * 32767).astype(np.int16)
        with wave.open(str(cache_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)  # 16-bit PCM
            wav.setframerate(int(sample_rate))
            wav.writeframes(pcm16.tobytes())
    except Exception as e:
        raise RuntimeError(
            f"Could not write Kokoro TTS audio to {cache_path}: {e}"
        ) from e
    if cache_path.stat().st_size == 0:
        raise RuntimeError(
            f"Kokoro TTS wrote an empty audio file to {cache_path}."
        )
    duration = _measure_wav_duration(cache_path)
    logger.info(
        f"Kokoro TTS (fallback) synthesized {len(clean_text)} chars "
        f"(voice={kokoro_voice}, {duration}s) -> {cache_path}"
    )
    return str(cache_path.resolve()), duration
