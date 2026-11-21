"""Telegram Bot API Key Management Commands for TubePilot AI.

Implements secure hot-swappable key rotation without redeploy:
- /keys list                         -> Masked display (first 6 + last 4 chars)
- /keys set NAME VALUE               -> Save + live test (auto-deletes user message for opsec)
- /keys test NAME|all                -> Live connectivity test
- /keys rotate NAME NEWVALUE         -> Archive old key, save new key
- /keys delete NAME                  -> Remove secret
- /keys pool NAME add|list|remove    -> Key pool management
- /keys export                       -> Base64 encrypted backup
- /keys import B64_DATA              -> Restore from backup
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from loguru import logger

from tools.secrets import (
    delete_secret,
    export_secrets,
    get_secret,
    import_secrets,
    list_secrets,
    mask_secret,
    rotate_secret,
    set_secret,
    test_secret,
    get_gemini_pool_status,
    add_gemini_pool_key,
    rotate_gemini_key,
)

__all__ = [
    "handle_keys_command",
]


def handle_keys_command(args: List[str]) -> Dict[str, Any]:
    """Execute /keys subcommands and return response text and auto-delete flag."""
    if not args:
        return {
            "text": (
                "🔐 *TubePilot AI Secrets Manager*\n\n"
                "Commands:\n"
                "• `/keys list` - View masked secrets\n"
                "• `/keys set NAME VALUE` - Set key (auto-deletes msg)\n"
                "• `/keys test NAME|all` - Test connectivity\n"
                "• `/keys rotate NAME NEW_VAL` - Rotate key\n"
                "• `/keys pool list|add|rotate` - Manage Gemini multi-key failover pool\n"
                "• `/keys delete NAME` - Delete secret\n"
                "• `/keys export` - Backup keys (base64)\n"
                "• `/keys import <b64>` - Restore backup"
            ),
            "delete_caller_msg": False,
        }

    subcmd = args[0].lower()

    # /keys list
    if subcmd == "list":
        secrets = list_secrets()
        if not secrets:
            return {"text": "No secrets currently configured in DynamoDB.", "delete_caller_msg": False}

        lines = ["🔐 *Configured API Secrets (Masked):*"]
        for k, v in sorted(secrets.items()):
            lines.append(f"• `{k}`: `{v}`")
        return {"text": "\n".join(lines), "delete_caller_msg": False}

    # /keys set NAME VALUE
    elif subcmd == "set":
        if len(args) < 3:
            return {"text": "❌ Usage: `/keys set KEY_NAME KEY_VALUE`", "delete_caller_msg": True}
        name = args[1].upper()
        val = args[2].strip()

        try:
            set_secret(name, val)
            live_ok = test_secret(name)
            status_emoji = "✅ (Live test passed)" if live_ok else "⚠️ (Saved, but test connection failed)"
            return {
                "text": f"✅ Secret `{name}` stored successfully.\nStatus: {status_emoji}\nMasked: `{mask_secret(val)}`",
                "delete_caller_msg": True,  # Critical security rule: auto-delete user message containing secret
            }
        except ValueError as ve:
            return {"text": f"❌ Validation Error: {ve}", "delete_caller_msg": True}

    # /keys test NAME|all
    elif subcmd == "test":
        target = args[1] if len(args) > 1 else "all"
        if target.lower() == "all":
            results = []
            for name in ["GEMINI_KEY", "OPENROUTER_KEY", "AWS_ACCESS_KEY_ID"]:
                ok = test_secret(name)
                results.append(f"• `{name}`: {'✅ Online' if ok else '❌ Offline/Unset'}")
            return {"text": "🔌 *Connectivity Test Results:*\n" + "\n".join(results), "delete_caller_msg": False}
        else:
            name = target.upper()
            ok = test_secret(name)
            return {
                "text": f"🔌 Test for `{name}`: {'✅ Connected' if ok else '❌ Connection Failed'}",
                "delete_caller_msg": False,
            }

    # /keys rotate NAME NEW_VAL
    elif subcmd == "rotate":
        if len(args) < 3:
            return {"text": "❌ Usage: `/keys rotate KEY_NAME NEW_VALUE`", "delete_caller_msg": True}
        name = args[1].upper()
        new_val = args[2].strip()
        try:
            rotate_secret(name, new_val)
            return {
                "text": f"🔄 Key `{name}` rotated.\nNew Value: `{mask_secret(new_val)}`",
                "delete_caller_msg": True,
            }
        except Exception as e:
            return {"text": f"❌ Rotation Failed: {e}", "delete_caller_msg": True}

    # /keys pool [list|add|rotate]
    elif subcmd == "pool":
        pool_action = args[1].lower() if len(args) > 1 else "list"

        if pool_action in ("list", "gemini"):
            status_list = get_gemini_pool_status()
            if not status_list:
                return {
                    "text": "💎 *Gemini API Key Pool:* Empty. Add keys using `/keys pool add gemini <API_KEY>`.",
                    "delete_caller_msg": False,
                }
            lines = [f"💎 *Gemini API Key Pool ({len(status_list)} keys configured):*"]
            for item in status_list:
                state_badge = "🟢 ACTIVE" if item["is_active"] else "⚪ STANDBY"
                health_badge = "Healthy"
                if item["status"] == "cooldown":
                    health_badge = f"⚠️ Cooldown ({item['cooldown_remaining_sec']}s left)"
                lines.append(f"• #{item['index']} `{item['name']}`: `{item['masked']}` [{state_badge} — {health_badge}]")
            lines.append("\n_Keys automatically failover on HTTP 429 rate limits or quota exhaustion._")
            return {"text": "\n".join(lines), "delete_caller_msg": False}

        elif pool_action == "add":
            if len(args) < 3 or (len(args) >= 3 and args[2].lower() == "gemini" and len(args) < 4):
                return {"text": "❌ Usage: `/keys pool add gemini <API_KEY>`", "delete_caller_msg": True}

            raw_key = args[3].strip() if args[2].lower() == "gemini" else args[2].strip()
            try:
                assigned_name = add_gemini_pool_key(raw_key)
                live_ok = test_secret(assigned_name)
                test_str = "✅ (Live test passed)" if live_ok else "⚠️ (Saved, connectivity check pending)"
                return {
                    "text": f"💎 Added Gemini API key to pool as `{assigned_name}`.\nStatus: {test_str}\nMasked: `{mask_secret(raw_key)}`",
                    "delete_caller_msg": True,
                }
            except ValueError as ve:
                return {"text": f"❌ Validation Error: {ve}", "delete_caller_msg": True}

        elif pool_action == "rotate":
            next_k = rotate_gemini_key()
            if next_k:
                return {
                    "text": f"🔄 Manually rotated Gemini pool to next key: `{mask_secret(next_k)}`",
                    "delete_caller_msg": False,
                }
            else:
                return {"text": "⚠️ Gemini pool has no available keys to rotate.", "delete_caller_msg": False}

        return {
            "text": "Usage: `/keys pool list` or `/keys pool add gemini <KEY>` or `/keys pool rotate gemini`",
            "delete_caller_msg": False,
        }

    # /keys delete NAME
    elif subcmd == "delete":
        if len(args) < 2:
            return {"text": "❌ Usage: `/keys delete KEY_NAME`", "delete_caller_msg": False}
        name = args[1].upper()
        delete_secret(name)
        return {"text": f"🗑️ Secret `{name}` removed from DynamoDB.", "delete_caller_msg": False}

    # /keys export
    elif subcmd == "export":
        b64_backup = export_secrets()
        return {
            "text": f"📦 *Encrypted Secrets Backup (Fernet-encrypted):*\n\n`{b64_backup}`\n\n_Store securely._",
            "delete_caller_msg": False,
        }

    # /keys import <b64>
    elif subcmd == "import":
        if len(args) < 2:
            return {"text": "❌ Usage: `/keys import <BASE64_STRING>`", "delete_caller_msg": True}
        try:
            import_secrets(args[1])
            return {"text": "✅ Secrets restored from backup successfully.", "delete_caller_msg": True}
        except Exception as e:
            return {"text": f"❌ Import failed: {e}", "delete_caller_msg": True}

    return {"text": f"Unknown keys command: `{subcmd}`", "delete_caller_msg": False}
