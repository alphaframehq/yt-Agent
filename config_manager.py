# LOCAL PRODUCTION PATCH — 2026-11-25
# Source: alphaframehq/yt-Agent main @ c39f42541b8e784dcf41c09583dc57227be07667 — config_manager.py
# Fixes: P0 audit #25 (region) + P1 #10 (task #27 partial): DynamoDB unavailable raises in production; in-memory fallback only with explicit ALLOW_IN_MEMORY_CONFIG=1 (default off); set_config write failures raise instead of logging 'updated'; validate_config() checks required active_models/budget/verify_chain at snapshot_config() startup; region from single env var AWS_REGION, no hardcoded region.
# Applied via PR #1 (branch fix/production-mocks), 2026-11-25.
# No secrets in this file — env placeholders / env-var names only.

"""
Hot-reloadable configuration manager for TubePilot AI.

Storage: DynamoDB ``yt_config`` table (config_id = "main").
Production behaviour: DynamoDB unavailable -> fail loudly (RuntimeError).
In-memory defaults are used ONLY when the explicit dev flag
``ALLOW_IN_MEMORY_CONFIG=1`` is set (default off).
AWS region comes from the single env var ``AWS_REGION`` (see .env.example);
there is no hardcoded region fallback.
Uses a 5-second TTL in-memory cache to avoid per-call round-trips.
"""

from __future__ import annotations

import copy
import os
import time
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# Default configuration — single source of truth
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "active_models": {
        "verify": "gemini/gemini-2.0-flash",
        "script": "gemini/gemini-2.0-flash",
        "image": "gemini/imagen-3.0-generate-001",
        "research": "tinyfish",
    },
    "verify_chain": [
        ["gemini", "gemini-2.0-flash"],
        ["gemini", "gemini-2.5-flash-lite"],
        ["openrouter", "deepseek/deepseek-chat:free"],
    ],
    "pillar_split": {"finance": 0.7, "tech": 0.3},
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

_TABLE_NAME: str = "yt_config"
_CONFIG_ID: str = "main"
_CACHE_TTL: float = 5.0  # seconds

# Single source of truth for the AWS region: env var AWS_REGION only.
# No hardcoded region anywhere in this file (boto3's own default chain
# is used only when AWS_REGION is unset, e.g. local AWS CLI config).
_AWS_REGION: str | None = os.environ.get("AWS_REGION") or None
# Explicit opt-in for local dev only. Default off: in production a
# DynamoDB failure must raise, never silently fall back to in-memory
# defaults (a pause/budget change that never persists looks applied).
_ALLOW_IN_MEMORY_CONFIG: bool = os.environ.get("ALLOW_IN_MEMORY_CONFIG", "").strip().lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# DynamoDB helpers (lazy-init, graceful fallback)
# ---------------------------------------------------------------------------

_dynamo_table: Any = None
_dynamo_available: bool | None = None  # None = not yet probed


def _get_table() -> Any:
    """Return the DynamoDB Table resource.

    Raises:
        RuntimeError: If DynamoDB is unavailable and the explicit dev
            flag ``ALLOW_IN_MEMORY_CONFIG`` is not set. In dev-flag mode
            only, returns ``None`` and callers fall back to in-memory
            defaults.
    """
    global _dynamo_table, _dynamo_available

    if _dynamo_available is False:
        if _ALLOW_IN_MEMORY_CONFIG:
            return None
        raise RuntimeError(
            "DynamoDB unavailable for config (see earlier log line); "
            "set ALLOW_IN_MEMORY_CONFIG=1 only for explicit local dev"
        )
    if _dynamo_table is not None:
        return _dynamo_table

    try:
        import boto3  # noqa: WPS433 — intentional lazy import

        _dynamo_table = boto3.resource("dynamodb", region_name=_AWS_REGION).Table(_TABLE_NAME)
        # Quick probe — will raise if creds/table missing
        _dynamo_table.table_status  # noqa: B018 — side-effect probe
        _dynamo_available = True
        logger.info("DynamoDB table '{}' connected", _TABLE_NAME)
        return _dynamo_table
    except Exception as exc:  # noqa: BLE001
        _dynamo_available = False
        if _ALLOW_IN_MEMORY_CONFIG:
            logger.warning(
                "DynamoDB unavailable ({}); ALLOW_IN_MEMORY_CONFIG=1, "
                "using in-memory defaults (dev only)",
                exc,
            )
            return None
        raise RuntimeError(f"DynamoDB unavailable for config: {exc}") from exc


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

_cache: dict[str, Any] = {}
_cache_ts: float = 0.0


def _cache_valid() -> bool:
    """Return *True* if the in-memory cache is still fresh."""
    return bool(_cache) and (time.monotonic() - _cache_ts < _CACHE_TTL)


def _invalidate_cache() -> None:
    """Force the next read to hit DynamoDB."""
    global _cache, _cache_ts
    _cache = {}
    _cache_ts = 0.0


# ---------------------------------------------------------------------------
# Internal I/O
# ---------------------------------------------------------------------------


_in_memory_config: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)


def _load_full_config() -> dict[str, Any]:
    """Fetch the full config dict from DynamoDB or return cached / in-memory fallback."""
    global _cache, _cache_ts

    if _cache_valid():
        return _cache

    table = _get_table()
    if table is not None:
        try:
            resp = table.get_item(Key={"config_id": _CONFIG_ID})
            item = resp.get("Item")
            if item is not None:
                # Strip the partition key — keep only config payload
                cfg = {k: v for k, v in item.items() if k != "config_id"}
                _cache = cfg
                _cache_ts = time.monotonic()
                logger.debug("Config loaded from DynamoDB")
                return _cache
        except Exception as exc:  # noqa: BLE001
            if not _ALLOW_IN_MEMORY_CONFIG:
                raise RuntimeError(f"DynamoDB config read failed: {exc}") from exc
            logger.warning("DynamoDB read failed ({}); using in-memory defaults (dev only)", exc)

    # Reached only when no stored config row exists yet (first boot) or
    # in dev-flag mode. In production _get_table() above already raised
    # instead of returning None, so this is never a silent failure fallback.
    if table is None and not _ALLOW_IN_MEMORY_CONFIG:
        raise RuntimeError(
            "DynamoDB config unavailable and ALLOW_IN_MEMORY_CONFIG is not set"
        )
    _cache = copy.deepcopy(_in_memory_config)
    _cache_ts = time.monotonic()
    return _cache


def _save_full_config(cfg: dict[str, Any]) -> None:
    """Persist the full config dict to DynamoDB.

    Raises:
        RuntimeError: If DynamoDB is unavailable (and the dev flag is
            not set) or the write fails. Callers must not report the
            change as applied when this raises.
    """
    global _in_memory_config
    table = _get_table()  # raises in production when DynamoDB is down
    if table is None:
        # Dev-flag mode only: _get_table() returns None instead of raising.
        _in_memory_config = copy.deepcopy(cfg)
        logger.warning("DynamoDB unavailable — config saved in-memory only (ALLOW_IN_MEMORY_CONFIG=1, dev only)")
        return
    try:
        item = {"config_id": _CONFIG_ID, **cfg}
        table.put_item(Item=item)
        _in_memory_config = copy.deepcopy(cfg)
        logger.info("Config saved to DynamoDB")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"DynamoDB config write failed: {exc}") from exc



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
    for a key missing remotely; in production a DynamoDB failure raises
    instead of silently serving defaults (dev-flag mode excepted).

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
    """Update a single config value and persist to DynamoDB.

    Args:
        key_path: Dot-separated path, e.g. ``"budget_ai_per_video"``.
        value:    New value to store.

    Raises:
        RuntimeError: If persisting to DynamoDB fails — the "updated"
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
    global _in_memory_config
    defaults = copy.deepcopy(DEFAULT_CONFIG)
    _in_memory_config = copy.deepcopy(defaults)
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
    """Update channel name in DynamoDB and in-memory cache."""
    name_clean = name.strip()
    if not name_clean:
        raise ValueError("Channel name cannot be empty.")
    set_config("channel_name", name_clean)
    logger.info("Channel name updated to: '{}'", name_clean)


def set_cta(cta_text: str, cta_type: str = "like_subscribe") -> None:
    """Update CTA text in DynamoDB and in-memory cache."""
    cta_clean = cta_text.strip()
    if not cta_clean:
        raise ValueError("CTA text cannot be empty.")
    set_config(f"cta.{cta_type}", cta_clean)
    logger.info("CTA '{}' updated to: '{}'", cta_type, cta_clean[:60])

