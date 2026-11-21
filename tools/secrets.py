"""
Encrypted secrets manager for TubePilot AI.

Encryption: Fernet with key derived from the ``MASTER_SECRET`` env var
(PBKDF2-HMAC-SHA256, 480 000 iterations).
Storage: DynamoDB ``yt_config`` table (config_id = "keys").
Env-var fallback for first-boot / local dev.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import time
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from loguru import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TABLE_NAME: str = "yt_config"
_SECRETS_ID: str = "keys"
_PBKDF2_ITERATIONS: int = 480_000
_SALT: bytes = b"tubepilot-ai-secrets-v1"  # static salt — rotated via MASTER_SECRET

# Known env-var names for first-boot fallback
_ENV_SECRET_NAMES: tuple[str, ...] = (
    "GEMINI_KEY",
    "OPENROUTER_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "PIXABAY_KEY",
    "ELEVEN_LABS_KEY",
    "TELEGRAM_BOT_TOKEN",
)

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
# DynamoDB helpers (lazy, graceful)
# ---------------------------------------------------------------------------

_dynamo_table: Any = None
_dynamo_available: bool | None = None


def _get_table() -> Any:
    """Return the DynamoDB Table resource, or *None* if unavailable."""
    global _dynamo_table, _dynamo_available

    if _dynamo_available is False:
        return None
    if _dynamo_table is not None:
        return _dynamo_table

    try:
        import boto3  # noqa: WPS433

        _dynamo_table = boto3.resource("dynamodb").Table(_TABLE_NAME)
        _dynamo_table.table_status  # noqa: B018
        _dynamo_available = True
        logger.info("Secrets DynamoDB table '{}' connected", _TABLE_NAME)
        return _dynamo_table
    except Exception as exc:  # noqa: BLE001
        _dynamo_available = False
        logger.warning(
            "DynamoDB unavailable for secrets ({}); env-var fallback active",
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------


def _derive_fernet_key() -> bytes:
    """Derive a 32-byte Fernet key from ``MASTER_SECRET`` env var.

    Raises:
        RuntimeError: If ``MASTER_SECRET`` is not set.
    """
    master = os.environ.get("MASTER_SECRET")
    if not master:
        raise RuntimeError(
            "MASTER_SECRET env var is required for secrets encryption"
        )
    kdf = PBKDF2HMAC(
        algorithm=SHA256(),
        length=32,
        salt=_SALT,
        iterations=_PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(master.encode()))


def _fernet() -> Fernet:
    """Return a Fernet instance keyed to ``MASTER_SECRET``."""
    return Fernet(_derive_fernet_key())


def _encrypt(plaintext: str) -> str:
    """Encrypt *plaintext* and return a URL-safe base-64 token string."""
    return _fernet().encrypt(plaintext.encode()).decode()


def _decrypt(token: str) -> str:
    """Decrypt a Fernet *token* string and return the plaintext."""
    return _fernet().decrypt(token.encode()).decode()


# ---------------------------------------------------------------------------
# DynamoDB secret store I/O
# ---------------------------------------------------------------------------


def _load_secrets_record() -> dict[str, str]:
    """Load the encrypted secrets record from DynamoDB.

    Returns an empty dict when DynamoDB is unavailable or the record
    does not exist.
    """
    table = _get_table()
    if table is None:
        return {}
    try:
        resp = table.get_item(Key={"config_id": _SECRETS_ID})
        item = resp.get("Item", {})
        return {k: v for k, v in item.items() if k != "config_id"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load secrets from DynamoDB: {}", exc)
        return {}


def _save_secrets_record(secrets: dict[str, str]) -> None:
    """Write the full encrypted secrets record to DynamoDB."""
    table = _get_table()
    if table is None:
        logger.warning("DynamoDB unavailable — secrets not persisted remotely")
        return
    try:
        item = {"config_id": _SECRETS_ID, **secrets}
        table.put_item(Item=item)
        logger.info("Secrets record persisted to DynamoDB")
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to save secrets to DynamoDB: {}", exc)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_secret(name: str, value: str) -> None:
    """Validate *value* against known rules for *name*.

    Raises:
        ValueError: When a rule is violated.
    """
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


def get_secret(name: str) -> Optional[str]:
    """Retrieve a decrypted secret by *name*.

    Resolution order:
    1. DynamoDB encrypted store.
    2. Environment variable (first-boot / local-dev fallback).

    Args:
        name: Secret identifier, e.g. ``"GEMINI_KEY"``.

    Returns:
        Decrypted secret string, or ``None`` if not found anywhere.
    """
    secrets = _load_secrets_record()
    token = secrets.get(name)
    if token is not None:
        try:
            plaintext = _decrypt(token)
            logger.debug("Secret '{}' loaded from DynamoDB", name)
            return plaintext
        except (InvalidToken, RuntimeError) as exc:
            logger.error("Failed to decrypt secret '{}': {}", name, exc)

    # Env-var fallback
    env_val = os.environ.get(name)
    if env_val:
        logger.debug("Secret '{}' loaded from env var (first-boot fallback)", name)
        return env_val

    logger.debug("Secret '{}' not found", name)
    return None


def set_secret(name: str, value: str) -> None:
    """Validate, encrypt, and persist a secret.

    Args:
        name:  Secret identifier.
        value: Raw secret value.

    Raises:
        ValueError: If the value fails validation rules.
    """
    _validate_secret(name, value)
    secrets = _load_secrets_record()
    secrets[name] = _encrypt(value)
    _save_secrets_record(secrets)
    logger.info("Secret '{}' saved ({})", name, mask_secret(value))


def delete_secret(name: str) -> None:
    """Remove a secret from the DynamoDB store.

    Args:
        name: Secret identifier to delete.
    """
    secrets = _load_secrets_record()
    if name in secrets:
        del secrets[name]
        _save_secrets_record(secrets)
        logger.info("Secret '{}' deleted", name)
    else:
        logger.warning("Secret '{}' not found — nothing to delete", name)


def list_secrets() -> dict[str, str]:
    """Return all stored secrets with masked values.

    Returns:
        Mapping of secret name → masked value.
    """
    secrets = _load_secrets_record()
    masked: dict[str, str] = {}
    for name, token in secrets.items():
        try:
            plaintext = _decrypt(token)
            masked[name] = mask_secret(plaintext)
        except (InvalidToken, RuntimeError):
            masked[name] = "<decryption-failed>"
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
    """
    value = get_secret(name)
    if value is None:
        logger.warning("Cannot test '{}' — secret not found", name)
        return False

    try:
        if name == "GEMINI_KEY":
            import httpx

            resp = httpx.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"key": value},
                timeout=10,
            )
            return resp.status_code == 200

        if name == "OPENROUTER_KEY":
            import httpx

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
                aws_secret_access_key=get_secret("AWS_SECRET_ACCESS_KEY") or "",
            )
            sts.get_caller_identity()
            return True

        if name == "PIXABAY_KEY":
            import httpx

            resp = httpx.get(
                "https://pixabay.com/api/",
                params={"key": value, "q": "test", "per_page": 3},
                timeout=10,
            )
            return resp.status_code == 200

        if name == "ELEVEN_LABS_KEY":
            import httpx

            resp = httpx.get(
                "https://api.elevenlabs.io/v1/voices",
                headers={"xi-api-key": value},
                timeout=10,
            )
            return resp.status_code == 200

        if name == "TELEGRAM_BOT_TOKEN":
            import httpx

            resp = httpx.get(
                f"https://api.telegram.org/bot{value}/getMe",
                timeout=10,
            )
            return resp.status_code == 200 and resp.json().get("ok", False)

        logger.info("No live test implemented for '{}'", name)
        return True  # no test available — assume valid

    except Exception as exc:  # noqa: BLE001
        logger.error("Live test for '{}' failed: {}", name, exc)
        return False


def rotate_secret(name: str, new_value: str) -> None:
    """Rotate a secret: archive the old value, then save the new one.

    The old value is stored under ``{name}__archived_{timestamp}``.

    Args:
        name:      Secret identifier.
        new_value: Replacement secret value.

    Raises:
        ValueError: If the new value fails validation.
    """
    _validate_secret(name, new_value)
    secrets = _load_secrets_record()

    # Archive old value (if any)
    old_token = secrets.get(name)
    if old_token is not None:
        archive_key = f"{name}__archived_{int(time.time())}"
        secrets[archive_key] = old_token
        logger.info("Old secret '{}' archived as '{}'", name, archive_key)

    secrets[name] = _encrypt(new_value)
    _save_secrets_record(secrets)
    logger.info("Secret '{}' rotated ({})", name, mask_secret(new_value))


def export_secrets() -> str:
    """Export all encrypted secrets as a base-64 encoded JSON string.

    The export contains the raw Fernet tokens — they can only be
    re-imported with the same ``MASTER_SECRET``.

    Returns:
        Base-64 encoded JSON payload.
    """
    secrets = _load_secrets_record()
    payload = json.dumps(secrets, separators=(",", ":"))
    encoded = base64.b64encode(payload.encode()).decode()
    logger.info("Exported {} secret(s)", len(secrets))
    return encoded


def import_secrets(b64_data: str) -> None:
    """Restore secrets from a base-64 encoded backup.

    Merges imported secrets into the current store (existing keys
    are overwritten).

    Args:
        b64_data: Base-64 encoded JSON payload from ``export_secrets()``.

    Raises:
        ValueError: If the data cannot be decoded / parsed.
    """
    try:
        raw = base64.b64decode(b64_data)
        incoming: dict[str, str] = json.loads(raw)
    except Exception as exc:
        raise ValueError(f"Invalid backup data: {exc}") from exc

    if not isinstance(incoming, dict):
        raise ValueError("Backup payload must be a JSON object")

    # Verify each token is decryptable
    for name, token in incoming.items():
        try:
            _decrypt(token)
        except (InvalidToken, RuntimeError) as exc:
            raise ValueError(
                f"Cannot decrypt secret '{name}' — MASTER_SECRET mismatch?"
            ) from exc

    secrets = _load_secrets_record()
    secrets.update(incoming)
    _save_secrets_record(secrets)
    logger.info("Imported {} secret(s)", len(incoming))
