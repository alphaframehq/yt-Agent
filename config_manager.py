"""
Hot-reloadable configuration manager for TubePilot AI.

Storage: DynamoDB ``yt_config`` table (config_id = "main").
Falls back to in-memory defaults when AWS / DynamoDB is unavailable.
Uses a 5-second TTL in-memory cache to avoid per-call round-trips.
"""

from __future__ import annotations

import copy
import time
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# Default configuration — single source of truth
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "active_models": {
        "verify": "bedrock/claude-3-5-haiku",
        "script": "gemini/gemini-2.0-flash",
        "image": "gemini/imagen-3.0-generate-001",
        "research": "tinyfish",
    },
    "verify_chain": [
        ["bedrock", "claude-3-5-haiku"],
        ["gemini", "gemini-2.0-flash"],
        ["gemini", "gemini-2.5-flash-lite"],
        ["openrouter", "deepseek/deepseek-chat:free"],
    ],
    "pillar_split": {"finance": 0.7, "tech": 0.3},
    "budget_ai_per_video": 0.30,
    "voice": "Joanna",
    "schedule": "0 4 * * *",
    "paused": False,
}

_TABLE_NAME: str = "yt_config"
_CONFIG_ID: str = "main"
_CACHE_TTL: float = 5.0  # seconds

# ---------------------------------------------------------------------------
# DynamoDB helpers (lazy-init, graceful fallback)
# ---------------------------------------------------------------------------

_dynamo_table: Any = None
_dynamo_available: bool | None = None  # None = not yet probed


def _get_table() -> Any:
    """Return the DynamoDB Table resource, or *None* if unavailable."""
    global _dynamo_table, _dynamo_available

    if _dynamo_available is False:
        return None
    if _dynamo_table is not None:
        return _dynamo_table

    try:
        import boto3  # noqa: WPS433 — intentional lazy import

        _dynamo_table = boto3.resource("dynamodb").Table(_TABLE_NAME)
        # Quick probe — will raise if creds/table missing
        _dynamo_table.table_status  # noqa: B018 — side-effect probe
        _dynamo_available = True
        logger.info("DynamoDB table '{}' connected", _TABLE_NAME)
        return _dynamo_table
    except Exception as exc:  # noqa: BLE001
        _dynamo_available = False
        logger.warning(
            "DynamoDB unavailable ({}); falling back to in-memory defaults",
            exc,
        )
        return None


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


def _load_full_config() -> dict[str, Any]:
    """Fetch the full config dict from DynamoDB or return cached / default."""
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
            logger.warning("DynamoDB read failed ({}); using defaults", exc)

    # Fall back to defaults
    _cache = copy.deepcopy(DEFAULT_CONFIG)
    _cache_ts = time.monotonic()
    return _cache


def _save_full_config(cfg: dict[str, Any]) -> None:
    """Persist the full config dict to DynamoDB."""
    table = _get_table()
    if table is None:
        logger.warning("DynamoDB unavailable — config saved in-memory only")
        return
    try:
        item = {"config_id": _CONFIG_ID, **cfg}
        table.put_item(Item=item)
        logger.info("Config saved to DynamoDB")
    except Exception as exc:  # noqa: BLE001
        logger.error("DynamoDB write failed: {}", exc)


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
]


def get_config(key_path: str) -> Any:
    """Return the config value at *key_path* (dot-notation).

    Uses a 5-second in-memory cache.  Falls back to ``DEFAULT_CONFIG``
    when DynamoDB is unavailable or the key is missing remotely.

    Args:
        key_path: Dot-separated path, e.g. ``"active_models.verify"``.

    Returns:
        The stored (or default) value.

    Raises:
        KeyError: If *key_path* is not found in either source.
    """
    cfg = _load_full_config()
    try:
        return _resolve_path(cfg, key_path)
    except KeyError:
        logger.debug(
            "Key '{}' missing in stored config; trying defaults", key_path
        )
        return _resolve_path(DEFAULT_CONFIG, key_path)


def set_config(key_path: str, value: Any) -> None:
    """Update a single config value and persist to DynamoDB.

    Args:
        key_path: Dot-separated path, e.g. ``"budget_ai_per_video"``.
        value:    New value to store.
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
    frozen = copy.deepcopy(cfg)
    logger.debug("Config snapshot created")
    return frozen


def reset_config() -> None:
    """Reset the configuration to ``DEFAULT_CONFIG`` and persist."""
    defaults = copy.deepcopy(DEFAULT_CONFIG)
    _save_full_config(defaults)
    _invalidate_cache()
    logger.info("Config reset to defaults")
