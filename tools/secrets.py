# No secrets in this file — env-var names only.
# 25 Nov 2026 (DynamoDB -> Google Sheets migration): secrets are NO
# longer stored anywhere in this repo's data store. DynamoDB is gone
# and NO secret is stored in Google Sheets. Every secret comes from
# the environment — in production, the GitHub repo secrets
# (Settings -> Secrets and variables -> Actions), passed into the
# runner as env vars; locally, the Codespace `.env`.
# get_secret() reads os.environ only and raises loudly when a secret
# is missing. The private _get_secret_optional() helper exists ONLY
# for genuine fallback/presence-check sites (LLM provider chain,
# GH_PAT checks, health diagnostics, best-effort notifications) —
# it must never be used to silently substitute a different secret.

"""
Environment-only secrets manager for TubePilot AI.

Resolution: ``os.environ`` only. There is no encrypted store, no
DynamoDB table and no Sheets tab holding secrets — a missing secret
raises ``RuntimeError`` (fail loudly) instead of falling back to
anything else. Rotation / setting / deleting a secret is done in the
GitHub repo settings, not through the bot: the ``/keys set`` /
``/keys delete`` / ``/keys rotate`` commands now explain that
instead of persisting anything.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

import httpx
from loguru import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GITHUB_SECRETS_HINT: str = (
    "Secrets are managed in the GitHub repo settings "
    "(Settings -> Secrets and variables -> Actions) — set the value "
    "there and re-run. The bot cannot store secrets anymore: DynamoDB "
    "was removed 25 Nov 2026 and no secret is ever written to Google Sheets."
)

# Known env-var names (used by list_secrets() and documentation).
_ENV_SECRET_NAMES: tuple[str, ...] = (
    "GEMINI_KEY",
    "GEMINI_KEY_POOL",
    "OPENROUTER_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "PIXABAY_KEY",
    "ELEVEN_LABS_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ADMIN_ID",
    "GH_PAT",
    "AGNES_API_KEY",
    "AGNES_BASE_URL",
    "TINYFISH_API_KEY",
    "FAL_KEY",
    "REPLICATE_API_TOKEN",
    "YT_CLIENT_ID",
    "YT_CLIENT_SECRET",
    "YT_REFRESH_TOKEN",
)

# Gemini API keys come in two formats, both valid:
# - legacy standard keys starting with "AIza"
# - auth keys issued by Google AI Studio since 28 May 2026, starting with "AQ."
_GEMINI_KEY_PREFIXES: tuple[str, ...] = ("AIza", "AQ.")


# ---------------------------------------------------------------------------
# Validation rules  {prefix | min-length}
# ---------------------------------------------------------------------------

_VALIDATION_RULES: dict[str, dict[str, Any]] = {
    "GEMINI_KEY": {"prefix": "AIza"},
    "OPENROUTER_KEY": {"prefix": "sk-or-"},
    "AWS_ACCESS_KEY_ID": {"min_length": 16},
    "AWS_SECRET_ACCESS_KEY": {"min_length": 16},
    "PIXABAY_KEY": {"min_length": 20},
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_secret(name: str, value: str) -> None:
    """Validate *value* against known rules for *name*.

    Raises:
        ValueError: When a rule is violated.
    """
    if name.startswith("GEMINI_KEY"):
        if name == "GEMINI_KEY_POOL":
            for part in value.split(","):
                part = part.strip()
                if part and (not part.startswith(_GEMINI_KEY_PREFIXES) or len(part) < 20):
                    raise ValueError(
                        "Every key in GEMINI_KEY_POOL must start with 'AIza' or 'AQ.' and be >= 20 characters"
                    )
            return
        if not value.startswith(_GEMINI_KEY_PREFIXES):
            raise ValueError(
                f"{name} must start with 'AIza' or 'AQ.' (got '{value[:4]}')"
            )
        if len(value) < 20:
            raise ValueError(
                f"{name} must be at least 20 characters (got {len(value)})"
            )
        return

    rules = _VALIDATION_RULES.get(name)
    if rules is None:
        return  # no rules for this key

    prefix = rules.get("prefix")
    if prefix and not value.startswith(prefix):
        raise ValueError(
            f"{name} must start with '{prefix}' (got '{value[:len(prefix)]}')"
        )

    min_len = rules.get("min_length")
    if min_len and len(value) < min_len:
        raise ValueError(
            f"{name} must be at least {min_len} characters (got {len(value)})"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "get_secret",
    "set_secret",
    "delete_secret",
    "list_secrets",
    "test_secret",
    "mask_secret",
    "rotate_secret",
    "export_secrets",
    "import_secrets",
    # Gemini Key Pool API
    "get_gemini_pool_keys",
    "get_active_gemini_key",
    "mark_gemini_key_failed",
    "add_gemini_pool_key",
    "get_gemini_pool_status",
    "rotate_gemini_key",
    "reset_gemini_pool",
]


def mask_secret(value: str) -> str:
    """Return a masked version of *value* showing the first 6 and last 4 chars.

    Args:
        value: The raw secret string.

    Returns:
        Masked string, e.g. ``"AIzaSy…ab12"``.
    """
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:6]}…{value[-4:]}"


def _get_secret_optional(name: str) -> Optional[str]:
    """Return the env value for *name*, or ``None`` if unset/empty.

    Private helper for genuine fallback / presence-check sites only:
    the LLM provider chain, GH_PAT checks, health diagnostics and
    best-effort notifications. Every other caller must use
    :func:`get_secret`, which raises on a missing secret.
    """
    value = os.environ.get(name)
    return value if value else None


def get_secret(name: str) -> str:
    """Retrieve a secret by *name* from the environment only.

    Args:
        name: Secret identifier, e.g. ``"GEMINI_KEY"``.

    Returns:
        The secret string from ``os.environ``.

    Raises:
        RuntimeError: If the secret is not set in the environment.
            There is no store to fall back to — DynamoDB was removed
            and secrets are never kept in Google Sheets.
    """
    value = _get_secret_optional(name)
    if value is not None:
        logger.debug("Secret '{}' loaded from env var", name)
        return value
    raise RuntimeError(f"Secret '{name}' is not set. {_GITHUB_SECRETS_HINT}")


def set_secret(name: str, value: str) -> None:
    """Validate *value* and direct the operator to GitHub repo settings.

    Secrets can no longer be stored from code — there is no secrets
    store anymore. This validates the value first (so a bad value
    still fails fast with ``ValueError``), then raises telling the
    operator where to set it.

    Raises:
        ValueError: If the value fails validation rules.
        RuntimeError: Always (after validation) — setting a secret
            from code is not supported anymore.
    """
    _validate_secret(name, value)
    raise RuntimeError(
        f"Cannot store secret '{name}' from code. {_GITHUB_SECRETS_HINT}"
    )


def delete_secret(name: str) -> None:
    """Direct the operator to GitHub repo settings to delete a secret.

    Raises:
        RuntimeError: Always — deleting a secret from code is not
            supported anymore.
    """
    raise RuntimeError(
        f"Cannot delete secret '{name}' from code. {_GITHUB_SECRETS_HINT}"
    )


def list_secrets() -> dict[str, str]:
    """Return all configured secrets (from env) with masked values.

    Only secret names that are actually set in the environment are
    listed. Values are masked; nothing secret is ever logged or
    returned in full.

    Returns:
        Mapping of secret name -> masked value.
    """
    names = set(_ENV_SECRET_NAMES)
    for env_name in os.environ:
        if env_name.startswith("GEMINI_KEY_"):
            names.add(env_name)
    masked: dict[str, str] = {}
    for name in sorted(names):
        value = _get_secret_optional(name)
        if value is not None:
            masked[name] = mask_secret(value)
    return masked


def test_secret(name: str) -> bool:
    """Run a lightweight connectivity / validity test for *name*.

    Supported providers: ``GEMINI_KEY``, ``OPENROUTER_KEY``,
    ``AWS_ACCESS_KEY_ID`` (STS), ``PIXABAY_KEY``, ``ELEVEN_LABS_KEY``,
    ``TELEGRAM_BOT_TOKEN``.

    Args:
        name: Secret identifier to test.

    Returns:
        ``True`` if the key appears valid and the service responds.
        ``False`` when the secret is unset or for providers with no
        live test implemented (never ``True`` by assumption).
    """
    value = _get_secret_optional(name)
    if value is None:
        logger.warning("Cannot test '{}' — secret not found", name)
        return False

    try:
        if name == "GEMINI_KEY":
            resp = httpx.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"key": value},
                timeout=10,
            )
            return resp.status_code == 200

        if name == "OPENROUTER_KEY":
            resp = httpx.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {value}"},
                timeout=10,
            )
            return resp.status_code == 200

        if name == "AWS_ACCESS_KEY_ID":
            import boto3

            sts = boto3.client(
                "sts",
                aws_access_key_id=value,
                aws_secret_access_key=_get_secret_optional("AWS_SECRET_ACCESS_KEY") or "",
            )
            sts.get_caller_identity()
            return True

        if name == "PIXABAY_KEY":
            resp = httpx.get(
                "https://pixabay.com/api/",
                params={"key": value, "q": "test", "per_page": 3},
                timeout=10,
            )
            return resp.status_code == 200

        if name == "ELEVEN_LABS_KEY":
            resp = httpx.get(
                "https://api.elevenlabs.io/v1/voices",
                headers={"xi-api-key": value},
                timeout=10,
            )
            return resp.status_code == 200

        if name == "TELEGRAM_BOT_TOKEN":
            resp = httpx.get(
                f"https://api.telegram.org/bot{value}/getMe",
                timeout=10,
            )
            return resp.status_code == 200 and resp.json().get("ok", False)

        logger.warning("No live test implemented for '{}' — returning False, not assuming valid", name)
        return False

    except Exception as exc:  # noqa: BLE001
        logger.error("Live test for '{}' failed: {}", name, exc)
        return False


def rotate_secret(name: str, new_value: str) -> None:
    """Validate *new_value* and direct the operator to GitHub repo settings.

    Raises:
        ValueError: If the new value fails validation.
        RuntimeError: Always (after validation) — rotating a secret
            from code is not supported anymore.
    """
    _validate_secret(name, new_value)
    raise RuntimeError(
        f"Cannot rotate secret '{name}' from code. {_GITHUB_SECRETS_HINT}"
    )


def export_secrets() -> str:
    """No longer supported — there is no encrypted secrets store to export.

    Raises:
        RuntimeError: Always.
    """
    raise RuntimeError(
        "Secrets export is no longer supported: secrets live in GitHub "
        "repo settings and there is no encrypted store to back up. "
        "Rotate a secret by updating it in the repo settings."
    )


def import_secrets(b64_data: str) -> None:
    """No longer supported — there is no encrypted secrets store to restore.

    Raises:
        RuntimeError: Always.
    """
    raise RuntimeError(
        "Secrets import is no longer supported: secrets live in GitHub "
        "repo settings and there is no encrypted store to restore into."
    )


# ---------------------------------------------------------------------------
# Gemini API Key Pool Management
# ---------------------------------------------------------------------------

_gemini_cooldowns: dict[str, float] = {}  # {key_string: cooldown_expiry_timestamp}
_gemini_active_idx: int = 0


def reset_gemini_pool() -> None:
    """Reset pool index and clear cooldowns (useful for tests or refresh)."""
    global _gemini_active_idx, _gemini_cooldowns
    _gemini_active_idx = 0
    _gemini_cooldowns.clear()


def get_gemini_pool_keys() -> list[str]:
    """Discover all configured Gemini API keys from the environment.

    Scans:
    1. GEMINI_KEY (primary default)
    2. GEMINI_KEY_POOL (comma-separated or JSON list of keys)
    3. GEMINI_KEY_1 .. GEMINI_KEY_20 (indexed standby keys, env vars)

    Returns:
        Deduplicated list of Gemini API keys in natural index order.
    """
    keys: list[str] = []

    # 1. Primary key
    primary = _get_secret_optional("GEMINI_KEY")
    if primary and primary.startswith(_GEMINI_KEY_PREFIXES):
        keys.append(primary)

    # 2. Key pool string (comma-separated or JSON list)
    pool_str = _get_secret_optional("GEMINI_KEY_POOL")
    if pool_str:
        try:
            parsed = json.loads(pool_str)
            if isinstance(parsed, list):
                for k in parsed:
                    if isinstance(k, str) and k.startswith(_GEMINI_KEY_PREFIXES) and k not in keys:
                        keys.append(k)
        except Exception:
            for part in pool_str.split(","):
                k = part.strip()
                if k.startswith(_GEMINI_KEY_PREFIXES) and k not in keys:
                    keys.append(k)

    # 3. Indexed standby keys from the environment
    for i in range(1, 21):
        env_val = _get_secret_optional(f"GEMINI_KEY_{i}")
        if env_val and env_val.startswith(_GEMINI_KEY_PREFIXES) and env_val not in keys:
            keys.append(env_val)

    return keys


def get_active_gemini_key() -> Optional[str]:
    """Retrieve the current active Gemini key, avoiding keys currently in cooldown.

    Returns:
        Active API key string, or None if no keys exist.
    """
    global _gemini_active_idx
    keys = get_gemini_pool_keys()
    if not keys:
        return _get_secret_optional("GEMINI_KEY")

    now = time.time()
    healthy_keys = [k for k in keys if _gemini_cooldowns.get(k, 0.0) <= now]

    if healthy_keys:
        if _gemini_active_idx >= len(healthy_keys):
            _gemini_active_idx = 0
        return healthy_keys[_gemini_active_idx]

    # If all keys are in cooldown, pick the one with the soonest expiring cooldown
    keys_by_expiry = sorted(keys, key=lambda k: _gemini_cooldowns.get(k, 0.0))
    return keys_by_expiry[0]


def mark_gemini_key_failed(
    key: str, error_status: int = 429, cooldown_sec: float | None = None
) -> Optional[str]:
    """Mark a Gemini key as rate-limited or failed, applying a cooldown and rotating.

    Args:
        key: The key string that failed
        error_status: HTTP status (429 rate limit, 403 quota/forbidden, 401 invalid)
        cooldown_sec: Optional custom cooldown in seconds. Defaults: 60s for 429, 3600s for 401/403.

    Returns:
        The next healthy active key, or None.
    """
    global _gemini_active_idx
    if cooldown_sec is None:
        if error_status == 429:
            cooldown_sec = 60.0  # 1 min cooldown for rate limits
        elif error_status in (401, 403):
            cooldown_sec = 3600.0  # 1 hour cooldown for quota or auth errors
        else:
            cooldown_sec = 30.0

    _gemini_cooldowns[key] = time.time() + cooldown_sec
    logger.warning(
        f"Gemini API key [{mask_secret(key)}] put into cooldown for {cooldown_sec:.0f}s (status {error_status})"
    )

    keys = get_gemini_pool_keys()
    now = time.time()
    healthy = [k for k in keys if _gemini_cooldowns.get(k, 0.0) <= now]
    if healthy:
        next_key = healthy[0]
        _gemini_active_idx = 0
        logger.info(f"Gemini key pool rotated active key -> [{mask_secret(next_key)}]")
        return next_key

    # If all in cooldown, pick soonest expiring
    keys_by_expiry = sorted(keys, key=lambda k: _gemini_cooldowns.get(k, 0.0))
    return keys_by_expiry[0] if keys_by_expiry else None


def rotate_gemini_key() -> Optional[str]:
    """Manually rotate to the next key in the pool."""
    global _gemini_active_idx
    keys = get_gemini_pool_keys()
    if not keys:
        return None
    now = time.time()
    healthy = [k for k in keys if _gemini_cooldowns.get(k, 0.0) <= now] or keys
    _gemini_active_idx = (_gemini_active_idx + 1) % len(healthy)
    next_key = healthy[_gemini_active_idx]
    logger.info(f"Gemini key pool manually rotated to: [{mask_secret(next_key or '')}]")
    return next_key


def add_gemini_pool_key(key: str) -> str:
    """Validate a new Gemini API key and direct the operator to GitHub settings.

    Keys can no longer be stored from code. This validates the format
    and, if the key is already in the pool, returns its assigned name;
    otherwise it raises telling the operator which repo-secret name
    to add (``GEMINI_KEY`` if unset, else the next free
    ``GEMINI_KEY_<n>``).

    Args:
        key: Raw Gemini API key (must start with 'AIza' or 'AQ.')

    Returns:
        The secret name assigned, e.g. 'GEMINI_KEY_2' (only when the
        key is already configured).

    Raises:
        ValueError: If the key format is invalid.
        RuntimeError: Always for a new key — add it in GitHub repo settings.
    """
    key = key.strip()
    if not key.startswith(_GEMINI_KEY_PREFIXES) or len(key) < 20:
        raise ValueError("Invalid Gemini API key format (must start with 'AIza' or 'AQ.' and be >= 20 chars)")

    existing_keys = get_gemini_pool_keys()
    if key in existing_keys:
        logger.info(f"Key [{mask_secret(key)}] is already present in Gemini pool.")
        if _get_secret_optional("GEMINI_KEY") == key:
            return "GEMINI_KEY"
        for i in range(1, 21):
            if _get_secret_optional(f"GEMINI_KEY_{i}") == key:
                return f"GEMINI_KEY_{i}"

    # Key is new: tell the operator exactly which repo-secret name to add.
    if not _get_secret_optional("GEMINI_KEY"):
        target_name = "GEMINI_KEY"
    else:
        idx = 1
        while _get_secret_optional(f"GEMINI_KEY_{idx}") is not None:
            idx += 1
        target_name = f"GEMINI_KEY_{idx}"

    raise RuntimeError(
        f"Cannot add a Gemini key from code — add it as the GitHub repo "
        f"secret '{target_name}' (Settings -> Secrets and variables -> "
        "Actions) and re-run. DynamoDB storage was removed 25 Nov 2026."
    )


def get_gemini_pool_status() -> list[dict[str, Any]]:
    """Return diagnostic status information for all keys in the Gemini pool."""
    keys = get_gemini_pool_keys()
    active_key = get_active_gemini_key()
    now = time.time()

    status_list = []
    for idx, k in enumerate(keys):
        cooldown_exp = _gemini_cooldowns.get(k, 0.0)
        in_cooldown = cooldown_exp > now
        remaining = round(cooldown_exp - now, 1) if in_cooldown else 0.0

        secret_name = "GEMINI_KEY"
        if _get_secret_optional("GEMINI_KEY") != k:
            for i in range(1, 21):
                if _get_secret_optional(f"GEMINI_KEY_{i}") == k:
                    secret_name = f"GEMINI_KEY_{i}"
                    break

        status_list.append({
            "index": idx + 1,
            "name": secret_name,
            "masked": mask_secret(k),
            "is_active": (k == active_key),
            "status": "cooldown" if in_cooldown else "healthy",
            "cooldown_remaining_sec": remaining,
        })

    return status_list
