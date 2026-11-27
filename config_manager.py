# 25 Nov 2026 (DynamoDB -> Google Sheets migration): config is now
# stored in the ``yt_config`` tab of the Google Spreadsheet
# (one row per top-level key, JSON-encoded values) instead of the
# DynamoDB ``yt_config`` table. Secrets are NOT stored here — they
# live in GitHub repo secrets / env vars (see tools/secrets.py).
# No secrets in this file — env placeholders / env-var names only.

"""
Hot-reloadable configuration manager for TubePilot AI.

Storage: Google Sheets ``yt_config`` tab (via ``db.sheets``).
Production behaviour: a Sheets failure raises loudly (RuntimeError).
There is no in-memory fallback in production — a config change that
never persists must never look applied.
Uses a 5-second TTL in-memory cache to avoid per-call round-trips.
"""

from __future__ import annotations

import copy
import time
from typing import Any

from loguru import logger

from db.sheets import _read_rows, _rewrite_tab

# ---------------------------------------------------------------------------
# Default configuration — single source of truth
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "active_models": {
        "verify": "gemini/gemini-3.8-flash",
        "script": "gemini/gemini-3.8-flash",
        "image": "gemini/imagen-3.0-generate-001",
        "research": "tinyfish",
    },
    "verify_chain": [
        ["gemini", "gemini-3.8-flash"],
        ["gemini", "gemini-3.5-flash-lite"],
        # 27 Nov 2026: "gemini-2.5-flash-lite" REMOVED from this chain —
        # it returns HTTP 404 (retired) on the free tier; run #20 died
        # on it in the Shorts fallback chain. Do NOT re-add it without
        # a live 200 check.
        # NOTE: a verify_chain row stored in the Sheets yt_config tab
        # overrides this default — update that row too for the live
        # run to pick the removal up.
        ["openrouter", "openrouter/free"],
    ],
    "pillar_split": {
        # 26 Nov 2026 niche correction: the channel is AI, not finance.
        # Editorial mix for the AI niche (fractions sum to 1.0). NOTE:
        # nothing in the pipeline computes a content mix from this —
        # it is read only for display (Telegram /status); the mix is
        # a planning aid for which AI angles to queue, not a
        # programmatic split.
        "news_tools": 0.35,
        "explainers_how_it_works": 0.25,
        "scams_risks": 0.15,
        "predictions_strategy": 0.15,
        "leaks_conspiracy": 0.10,
    },
    "budget_ai_per_video": 0.30,
    "voice": "Joanna",
    "schedule": "0 4 * * *",
    "paused": False,
    "channel_name": "TubePilot AI",
    "cta": {
        "like_subscribe": "If you enjoy deep-dive architectural teardowns and institutional financial models, hit subscribe and ring the bell for our daily breakdowns.",
        "midroll": "If this breakdown brings clarity to complex fintech systems, drop a like below to support independent technical analysis.",
        "outro": "Subscribe to {channel_name} for weekly engineering deep dives into the plumbing of global finance.",
        "comment_prompt": "What fintech architecture or P&L model should we analyze next? Leave your thoughts in the comments below.",
    },
}

_CONFIG_TAB: str = "yt_config"
_CACHE_TTL: float = 5.0  # seconds


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

_cache: dict[str, Any] = {}
_cache_ts: float = 0.0


def _cache_valid() -> bool:
    """Return *True* if the in-memory cache is still fresh."""
    return bool(_cache) and (time.monotonic() - _cache_ts < _CACHE_TTL)


def _invalidate_cache() -> None:
    """Force the next read to hit Google Sheets."""
    global _cache, _cache_ts
    _cache = {}
    _cache_ts = 0.0


# ---------------------------------------------------------------------------
# Internal I/O
# ---------------------------------------------------------------------------


def _load_full_config() -> dict[str, Any]:
    """Fetch the full config dict from the Sheets ``yt_config`` tab.

    Returns the stored config merged over ``DEFAULT_CONFIG`` (stored
    keys win; missing keys fall back to defaults). An empty tab means
    first boot — defaults are returned (not yet persisted).

    Raises:
        RuntimeError: If Google Sheets cannot be reached — never a
            silent fallback to defaults in production.
    """
    global _cache, _cache_ts

    if _cache_valid():
        return _cache

    # Raises RuntimeError on any Sheets failure (fail loudly).
    rows = _read_rows(_CONFIG_TAB)
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    for row in rows:
        key = row.get("key")
        value = row.get("value_json")
        # An empty value cell reads back as None — treat it as "no
        # value stored" so it cannot wipe out a default.
        if key is not None and value is not None:
            # 26 Nov 2026: a hand-edited / corrupt cell can decode to
            # the wrong type (e.g. active_models stored as a plain
            # string instead of a dict). Applying it verbatim used to
            # crash validate_config() or, worse, downstream code with
            # an AttributeError far from the cause. A stored value
            # whose type disagrees with the default is ignored (with
            # a warning) and the default is kept, so a bad row can
            # never crash a run. Unknown keys (not in DEFAULT_CONFIG)
            # are still accepted as-is.
            if key in DEFAULT_CONFIG and not _value_matches_default(key, value):
                logger.warning(
                    "Ignoring yt_config row '{}': stored value {!r} "
                    "has the wrong type (expected {}); keeping default.",
                    key,
                    value,
                    type(DEFAULT_CONFIG[key]).__name__,
                )
                continue
            cfg[key] = value
    _cache = cfg
    _cache_ts = time.monotonic()
    logger.debug("Config loaded from Google Sheets")
    return _cache


def _value_matches_default(key: str, value: Any) -> bool:
    """Return True when *value* has the type ``DEFAULT_CONFIG[key]`` has.

    Numbers are interchangeable (int/float) except that ``bool`` is
    never a valid number here. Dicts/lists must match exactly; a
    string default only accepts a string.
    """
    default = DEFAULT_CONFIG[key]
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, dict):
        return isinstance(value, dict)
    if isinstance(default, list):
        return isinstance(value, list)
    if isinstance(default, str):
        return isinstance(value, str)
    return True


def _save_full_config(cfg: dict[str, Any]) -> None:
    """Persist the full config dict to the Sheets ``yt_config`` tab.

    Raises:
        RuntimeError: If persisting to Google Sheets fails. Callers
            must not report the change as applied when this raises.
    """
    rows = [{"key": k, "value_json": v} for k, v in cfg.items()]
    # Raises RuntimeError on any Sheets failure.
    _rewrite_tab(_CONFIG_TAB, rows)
    _invalidate_cache()
    logger.info("Config saved to Google Sheets")


# ---------------------------------------------------------------------------
# Dot-notation helpers
# ---------------------------------------------------------------------------


def _resolve_path(data: dict[str, Any], key_path: str) -> Any:
    """Traverse *data* by dot-separated *key_path* and return the value.

    Raises ``KeyError`` if the path does not exist.
    """
    parts = key_path.split(".")
    current: Any = data
    for part in parts:
        if isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(f"Cannot descend into non-dict at '{part}'")
    return current


def _set_nested(data: dict[str, Any], key_path: str, value: Any) -> None:
    """Set a value inside *data* at the given dot-separated *key_path*.

    Intermediate dicts are created if they don't exist.
    """
    parts = key_path.split(".")
    current = data
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "DEFAULT_CONFIG",
    "get_config",
    "set_config",
    "snapshot_config",
    "reset_config",
    "validate_config",
]


_UNSET = object()


def get_config(key_path: str, default: Any = _UNSET) -> Any:
    """Return the config value at *key_path* (dot-notation).

    Uses a 5-second in-memory cache. Falls back to ``DEFAULT_CONFIG``
    for a key missing remotely; a Google Sheets failure raises
    instead of silently serving defaults.

    Args:
        key_path: Dot-separated path, e.g. ``"active_models.verify"``.
        default: Optional fallback if key is missing from config and defaults.

    Returns:
        The stored (or default) value.

    Raises:
        KeyError: If *key_path* is not found and no default is provided.
    """
    cfg = _load_full_config()
    try:
        return _resolve_path(cfg, key_path)
    except KeyError:
        try:
            return _resolve_path(DEFAULT_CONFIG, key_path)
        except KeyError:
            if default is not _UNSET:
                return default
            raise


def set_config(key_path: str, value: Any) -> None:
    """Update a single config value and persist to Google Sheets.

    Args:
        key_path: Dot-separated path, e.g. ``"budget_ai_per_video"``.
        value:    New value to store.

    Raises:
        RuntimeError: If persisting to Google Sheets fails — the "updated"
            log line below is only reached after a successful write.
    """
    cfg = _load_full_config()
    cfg = copy.deepcopy(cfg)
    _set_nested(cfg, key_path, value)
    _save_full_config(cfg)
    _invalidate_cache()
    logger.info("Config '{}' updated to {!r}", key_path, value)


def snapshot_config() -> dict[str, Any]:
    """Return a deep-copied, frozen snapshot of the full configuration.

    Intended for use at pipeline start so that subsequent config changes
    do not create race conditions within a running pipeline.

    Returns:
        A deep copy of the current configuration dict.
    """
    cfg = _load_full_config()
    validate_config(cfg)
    frozen = copy.deepcopy(cfg)
    logger.debug("Config snapshot created")
    return frozen


def reset_config() -> None:
    """Reset the configuration to ``DEFAULT_CONFIG`` and persist."""
    defaults = copy.deepcopy(DEFAULT_CONFIG)
    _save_full_config(defaults)
    _invalidate_cache()
    logger.info("Config reset to defaults")


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

_REQUIRED_MODEL_KEYS: tuple[str, ...] = ("verify", "script", "image")


def validate_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate required keys/models in *cfg* (or the loaded config).

    Called from ``snapshot_config()`` so a misconfigured run fails at
    pipeline startup instead of mid-pipeline. Required keys are taken
    from the original ``DEFAULT_CONFIG`` shape: ``active_models`` must
    name a model for verify/script/image, ``verify_chain`` must be
    non-empty, and ``budget_ai_per_video`` must be a number.

    Raises:
        ValueError: Listing every missing/empty required key.
    """
    cfg = cfg if cfg is not None else _load_full_config()
    missing: list[str] = []
    models = cfg.get("active_models")
    if not isinstance(models, dict):
        missing.append("active_models")
    else:
        for key in _REQUIRED_MODEL_KEYS:
            if not models.get(key):
                missing.append(f"active_models.{key}")
    budget = cfg.get("budget_ai_per_video")
    if budget is None:
        missing.append("budget_ai_per_video")
    elif not isinstance(budget, (int, float)):
        missing.append("budget_ai_per_video (must be a number)")
    if not cfg.get("verify_chain"):
        missing.append("verify_chain")
    if missing:
        raise ValueError(f"Invalid/incomplete config; missing: {', '.join(missing)}")
    return cfg


# ---------------------------------------------------------------------------
# Channel Branding & Call-To-Action (CTA) Helpers
# ---------------------------------------------------------------------------

def get_channel_branding() -> dict[str, Any]:
    """Retrieve configured channel name and call-to-action (CTA) templates."""
    name = get_config("channel_name", default="TubePilot AI")
    cta = get_config("cta", default=DEFAULT_CONFIG["cta"])
    if isinstance(cta, str):
        cta = {
            "like_subscribe": cta,
            "outro": cta,
            "midroll": cta,
            "comment_prompt": "Leave your thoughts in the comments.",
        }
    return {
        "channel_name": name,
        "cta": cta,
    }


def set_channel_name(name: str) -> None:
    """Update channel name in Google Sheets and in-memory cache."""
    name_clean = name.strip()
    if not name_clean:
        raise ValueError("Channel name cannot be empty.")
    set_config("channel_name", name_clean)
    logger.info("Channel name updated to: '{}'", name_clean)


def set_cta(cta_text: str, cta_type: str = "like_subscribe") -> None:
    """Update CTA text in Google Sheets and in-memory cache."""
    cta_clean = cta_text.strip()
    if not cta_clean:
        raise ValueError("CTA text cannot be empty.")
    set_config(f"cta.{cta_type}", cta_clean)
    logger.info("CTA '{}' updated to: '{}'", cta_type, cta_clean[:60])
