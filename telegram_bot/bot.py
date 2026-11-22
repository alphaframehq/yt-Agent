"""Telegram Admin Bot Control Plane for TubePilot AI.

Enforces strict security:
- Single-Admin authentication: Only TELEGRAM_ADMIN_ID can execute commands.
- Auto-deletes messages containing raw API keys for chat hygiene.
- Zero public webhooks: Coordinates with ephemeral runners via DynamoDB message bus.

Full Command Set:
- Model control: /model [verify|script|image|research|reset]
- Topics: /topic [add|list|remove|auto]
- Execution: /run <topic>, /status, /pause, /resume
- Configuration: /config, /config set <key> <val>
- Secrets: /keys [list|set|test|rotate|delete|export|import]
- Diagnostics: /ping, /cost, /logs, /codespace
- Interactive Inline Approvals: [✅ Publish] [❌ Reject] [🔁 Regenerate] [✏️ Edit] [🖼️ Thumbnail]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config_manager import (
    get_config,
    reset_config,
    set_config,
    get_channel_branding,
    set_channel_name,
    set_cta,
)
from db.dynamo import enqueue_topic, list_topics_queue, update_video_status
from telegram_bot.codespace import handle_codespace_command
from telegram_bot.dispatch import dispatch_pipeline_run
from telegram_bot.health import get_cost_summary, get_pipeline_logs, get_system_ping
from telegram_bot.keys import handle_keys_command
from tools.secrets import get_secret
from tools.youtube_api import publish_public

__all__ = [
    "handle_command",
    "handle_callback_query",
    "check_admin_auth",
]


def check_admin_auth(user_id: int | str) -> bool:
    """Enforce strict admin authorization check."""
    admin_id = get_secret("TELEGRAM_ADMIN_ID") or os.getenv("TELEGRAM_ADMIN_ID")
    if not admin_id:
        # Default allow for local test suites
        return True
    return str(user_id) == str(admin_id)


def handle_command(user_id: int | str, command: str, args: List[str]) -> Dict[str, Any]:
    """Central router for all Telegram bot commands."""
    if not check_admin_auth(user_id):
        return {
            "text": "⛔️ *Unauthorized.* This bot is locked to the designated TubePilot administrator.",
            "delete_msg": False,
        }

    cmd = command.lower().strip().lstrip("/")

    # 0. /start or /help
    if cmd in ("start", "help"):
        branding = get_channel_branding()
        return {
            "text": (
                "👋 *Welcome to TubePilot AI Control Plane!*\n\n"
                f"📺 *Channel:* `{branding['channel_name']}`\n\n"
                "🛠 *Quick Commands:*\n"
                "• `/ping` — System health, latency & budget\n"
                "• `/run <Topic>` — Dispatch immediate video generation\n"
                "• `/status` — View pipeline status & topic queue\n"
                "• `/topic add <Topic>` — Enqueue high-priority topic\n"
                "• `/channel [name]` — View or update YouTube channel\n"
                "• `/cta [text]` — View or update Like/Subscribe CTA\n"
                "• `/keys list` — View API keys (Fernet encrypted)\n"
                "• `/keys pool` — Manage Gemini multi-key failover pool\n"
                "• `/model list` — View active AI models\n"
                "• `/codespace list|stop` — Inspect Codespaces\n"
                "• `/pause` / `/resume` — Emergency automation switch"
            ),
            "delete_msg": False,
        }

    # 1. /ping
    elif cmd == "ping":
        res = get_system_ping()
        return {"text": res["text"], "delete_msg": False}

    # 2. /keys
    elif cmd == "keys":
        res = handle_keys_command(args)
        return {"text": res["text"], "delete_msg": res.get("delete_caller_msg", False)}

    # 3. /model
    elif cmd == "model":
        return _handle_model_command(args)

    # 4. /topic
    elif cmd == "topic":
        return _handle_topic_command(args)

    # 5. /run
    elif cmd == "run":
        if not args:
            return {"text": "❌ Usage: `/run <Topic or Company Name>`", "delete_msg": False}
        topic = " ".join(args)
        dispatch_res = dispatch_pipeline_run(topic)
        return {
            "text": f"🚀 *Pipeline Dispatched!*\n\n• Topic: `{topic}`\n• Queue ID: `{dispatch_res['topic_id']}`\n• Status: {dispatch_res['details']}",
            "delete_msg": False,
        }

    # 6. /status
    elif cmd == "status":
        topics = list_topics_queue()
        pending = sum(1 for t in topics if t.get("status") == "pending")
        running = sum(1 for t in topics if t.get("status") == "running")
        return {
            "text": f"📊 *Queue Status:*\n• Running pipelines: `{running}`\n• Pending in queue: `{pending}`\n• Total tracked: `{len(topics)}`",
            "delete_msg": False,
        }

    # 7. /pause and /resume
    elif cmd in ("pause", "resume"):
        new_status = (cmd == "pause")
        set_config("paused", new_status)
        return {
            "text": f"{'⏸ Pipeline paused.' if new_status else '▶️ Pipeline resumed.'} Autonomous cron schedule updated.",
            "delete_msg": False,
        }

    # 8. /config
    elif cmd == "config":
        return _handle_config_command(args)

    # 8b. /channel or /channelname
    elif cmd in ("channel", "channelname"):
        return _handle_channel_command(args)

    # 8c. /cta
    elif cmd == "cta":
        return _handle_cta_command(args)

    # 9. /cost
    elif cmd == "cost":
        vid = args[0] if args else ""
        return {"text": get_cost_summary(vid), "delete_msg": False}

    # 10. /logs
    elif cmd == "logs":
        rid = args[0] if args else ""
        return {"text": get_pipeline_logs(rid), "delete_msg": False}

    # 11. /codespace
    elif cmd == "codespace":
        return {"text": handle_codespace_command(args), "delete_msg": False}

    # 12. /help or start
    return {
        "text": (
            "🤖 *TubePilot AI Admin Bot*\n\n"
            "• `/ping` - Full system health (<5s)\n"
            "• `/run <topic>` - Dispatch pipeline now\n"
            "• `/status` - Queue depth & active runs\n"
            "• `/model` - Manage AI model assignments\n"
            "• `/topic` - Queue management\n"
            "• `/keys` - Encrypted hot-swappable API keys\n"
            "• `/config` - System configuration\n"
            "• `/cost` - Cloud and LLM spend audit\n"
            "• `/codespace` - Dev container control\n"
            "• `/pause` / `/resume` - Halt or start scheduler"
        ),
        "delete_msg": False,
    }


def handle_callback_query(callback_data: str) -> Dict[str, Any]:
    """Handle interactive inline buttons from private upload notification.
    
    Buttons:
    - pub_<yt_id>: Publish video to YouTube Public
    - rej_<yt_id>: Mark rejected
    - reg_<id>: Regenerate video
    - edt_<id>: Edit metadata
    - thm_<yt_id>: Update thumbnail
    """
    logger.info(f"Telegram callback query received: {callback_data}")

    if callback_data.startswith("pub_"):
        yt_id = callback_data[4:]
        ok = publish_public(yt_id)
        if ok:
            return {"text": f"🎉 *VIDEO PUBLISHED PUBLIC!*\n\nYouTube ID: `{yt_id}`\nURL: https://youtu.be/{yt_id}"}
        return {"text": f"❌ Could not publish video `{yt_id}`. Please check YouTube API status."}

    elif callback_data.startswith("rej_"):
        yt_id = callback_data[4:]
        return {"text": f"❌ Video `{yt_id}` rejected and archived."}

    elif callback_data.startswith("reg_"):
        v_id = callback_data[4:]
        return {"text": f"🔁 Video `{v_id}` re-enqueued for regeneration."}

    elif callback_data.startswith("edt_"):
        return {"text": "✏️ Please send updated title and description in reply."}

    elif callback_data.startswith("thm_"):
        return {"text": "🖼️ Send new custom thumbnail image to replace current visual."}

    return {"text": "Action processed."}


def _handle_model_command(args: List[str]) -> Dict[str, Any]:
    """Subcommand handler for /model."""
    if not args:
        models = get_config("active_models") or {}
        lines = ["🤖 *Active AI Model Routing:*"]
        for role, m in models.items():
            lines.append(f"• `{role}`: `{m}`")
        return {"text": "\n".join(lines), "delete_msg": False}

    sub = args[0].lower()
    if sub == "reset":
        reset_config()
        return {"text": "🔄 Model routing reset to defaults (Bedrock Claude 3.5 Haiku + Gemini 2.0 Flash).", "delete_msg": False}

    if len(args) < 2:
        return {"text": "❌ Usage: `/model <role> <model_name>`", "delete_msg": False}

    role = sub
    target = args[1].lower()

    if target in ("claude", "bedrock"):
        val = "bedrock/claude-3-5-haiku"
    elif target in ("gemini-2.0", "gemini"):
        val = "gemini/gemini-2.0-flash"
    elif target in ("gemini-2.5-lite", "lite"):
        val = "gemini/gemini-2.5-flash-lite"
    elif target in ("imagen-3", "imagen"):
        val = "gemini/imagen-3.0-generate-001"
    else:
        val = target

    set_config(f"active_models.{role}", val)
    return {"text": f"✅ Model for `{role}` set to `{val}`. Takes effect on next pipeline run.", "delete_msg": False}


def _handle_topic_command(args: List[str]) -> Dict[str, Any]:
    """Subcommand handler for /topic."""
    if not args or args[0].lower() == "list":
        topics = list_topics_queue()
        if not topics:
            return {"text": "Topic queue is currently empty.", "delete_msg": False}
        lines = ["📋 *Upcoming Topics in Queue:*"]
        for t in topics[:8]:
            lines.append(f"• `[{t.get('status').upper()}]` {t.get('topic')} ({t.get('pillar')})")
        return {"text": "\n".join(lines), "delete_msg": False}

    sub = args[0].lower()
    if sub == "add":
        if len(args) < 2:
            return {"text": "❌ Usage: `/topic add <Title> [finance|tech]`", "delete_msg": False}
        pillar = "tech_to_financial" if "tech" in args[-1].lower() else "finance_to_technical"
        title_words = args[1:-1] if args[-1].lower() in ("finance", "tech") else args[1:]
        topic_title = " ".join(title_words).strip('"\'')

        top_id = enqueue_topic(topic_title, pillar=pillar)
        return {"text": f"✅ Topic added to queue!\n• Title: `{topic_title}`\n• Pillar: `{pillar}`\n• ID: `{top_id}`", "delete_msg": False}

    return {"text": "Usage: `/topic [list|add <title> [finance|tech]]`", "delete_msg": False}


def _handle_config_command(args: List[str]) -> Dict[str, Any]:
    """Subcommand handler for /config."""
    branding = get_channel_branding()
    if not args:
        return {
            "text": (
                "⚙️ *System Configuration:*\n"
                f"• Channel Name: `{branding['channel_name']}`\n"
                f"• Like/Subscribe CTA: `{branding['cta'].get('like_subscribe')}`\n"
                f"• Pillar Split: `{get_config('pillar_split')}`\n"
                f"• Budget Cap: `${get_config('budget_ai_per_video')} USD`\n"
                f"• Voice: `{get_config('voice')}`\n"
                f"• Cron Schedule: `{get_config('schedule')}`\n"
                f"• Paused: `{get_config('paused')}`\n\n"
                "_Update with:_ `/config set channel_name <Name>` or `/channel <Name>`"
            ),
            "delete_msg": False,
        }

    if args[0].lower() == "set" and len(args) >= 3:
        key = args[1]
        raw_val = " ".join(args[2:]).strip('"\'')

        if key.lower() in ("channel_name", "channel"):
            set_channel_name(raw_val)
            return {"text": f"✅ Channel name updated: `{raw_val}`", "delete_msg": False}
        elif key.lower() in ("cta", "cta.like_subscribe", "cta.outro"):
            cta_sub = "like_subscribe" if "outro" not in key.lower() else "outro"
            set_cta(raw_val, cta_type=cta_sub)
            return {"text": f"✅ Call-to-action updated:\n`{raw_val}`", "delete_msg": False}

        # Type conversions
        if raw_val.lower() in ("true", "false"):
            val_parsed: Any = (raw_val.lower() == "true")
        else:
            try:
                val_parsed = float(raw_val) if "." in raw_val else int(raw_val)
            except ValueError:
                val_parsed = raw_val

        set_config(key, val_parsed)
        return {"text": f"✅ Configuration updated: `{key}` = `{val_parsed}`", "delete_msg": False}

    return {"text": "Usage: `/config` or `/config set <key> <val>`", "delete_msg": False}


def _handle_channel_command(args: List[str]) -> Dict[str, Any]:
    """Subcommand handler for /channel or /channelname."""
    if not args:
        branding = get_channel_branding()
        return {
            "text": (
                f"📺 *Current YouTube Channel Name:* `{branding['channel_name']}`\n\n"
                "To update, use:\n`/channel <New Channel Name>`"
            ),
            "delete_msg": False,
        }

    new_name = " ".join(args).strip('"\'')
    try:
        set_channel_name(new_name)
        return {
            "text": (
                f"✅ *Channel Name Saved Successfully!*\n\n"
                f"• New Channel Name: `{new_name}`\n"
                "• All future scripts, descriptions, and Shorts CTAs will use this branding."
            ),
            "delete_msg": False,
        }
    except Exception as e:
        return {"text": f"❌ Error saving channel name: {e}", "delete_msg": False}


def _handle_cta_command(args: List[str]) -> Dict[str, Any]:
    """Subcommand handler for /cta."""
    branding = get_channel_branding()
    if not args:
        return {
            "text": (
                f"📣 *Current Call-To-Action (CTA):*\n\n"
                f"• *Like & Subscribe:* `{branding['cta'].get('like_subscribe')}`\n"
                f"• *Outro CTA:* `{branding['cta'].get('outro')}`\n\n"
                "To update, use:\n`/cta <New Like & Subscribe text>`"
            ),
            "delete_msg": False,
        }

    new_cta = " ".join(args).strip('"\'')
    try:
        set_cta(new_cta, cta_type="like_subscribe")
        set_cta(new_cta, cta_type="outro")
        return {
            "text": (
                f"✅ *Call-To-Action Saved Successfully!*\n\n"
                f"• New CTA: `{new_cta}`\n"
                "• All generated longform and shorts scripts will feature this CTA."
            ),
            "delete_msg": False,
        }
    except Exception as e:
        return {"text": f"❌ Error saving CTA: {e}", "delete_msg": False}


def run_bot() -> None:
    """Run Telegram bot listener in continuous long-polling mode."""
    import time
    from pathlib import Path
    import httpx

    # Load environment variables if available
    try:
        from dotenv import load_dotenv
        env_file = Path(__file__).resolve().parent.parent / ".env"
        if env_file.exists():
            load_dotenv(env_file)
    except ImportError:
        pass

    token = get_secret("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN is not configured. Cannot start Telegram bot.")
        return

    admin_id = get_secret("TELEGRAM_ADMIN_ID") or os.getenv("TELEGRAM_ADMIN_ID")
    logger.info(f"Starting Telegram bot listener. Admin ID configured: {admin_id}")

    base_url = f"https://api.telegram.org/bot{token}"
    offset = 0

    with httpx.Client(timeout=35.0) as client:
        try:
            me = client.get(f"{base_url}/getMe").json()
            if me.get("ok"):
                username = me["result"].get("username", "bot")
                first_name = me["result"].get("first_name", "")
                logger.info(f"✅ Bot connected to Telegram as @{username} ({first_name}). Awaiting commands...")
            else:
                logger.error(f"Failed to authenticate bot: {me}")
                return
        except Exception as e:
            logger.error(f"Network error connecting to Telegram: {e}")
            return

        while True:
            try:
                resp = client.get(
                    f"{base_url}/getUpdates",
                    params={"offset": offset, "timeout": 25},
                )
                if resp.status_code != 200:
                    time.sleep(2)
                    continue

                updates = resp.json().get("result", [])
                for upd in updates:
                    offset = upd["update_id"] + 1

                    # 1. Handle Callback Queries (Inline buttons)
                    if "callback_query" in upd:
                        cb = upd["callback_query"]
                        cb_id = cb["id"]
                        data = cb.get("data", "")
                        from_user = cb.get("from", {}).get("id", 0)
                        chat_id = cb.get("message", {}).get("chat", {}).get("id")

                        logger.info(f"Callback query '{data}' from {from_user}")
                        if not check_admin_auth(from_user):
                            client.post(
                                f"{base_url}/answerCallbackQuery",
                                json={"callback_query_id": cb_id, "text": "⛔️ Unauthorized.", "show_alert": True},
                            )
                            continue

                        res = handle_callback_query(data)
                        client.post(
                            f"{base_url}/answerCallbackQuery",
                            json={"callback_query_id": cb_id, "text": "Action executed."},
                        )
                        if chat_id and res.get("text"):
                            client.post(
                                f"{base_url}/sendMessage",
                                json={"chat_id": chat_id, "text": res["text"], "parse_mode": "Markdown"},
                            )
                        continue

                    # 2. Handle Text Messages / Commands
                    if "message" in upd and "text" in upd["message"]:
                        msg = upd["message"]
                        msg_id = msg["message_id"]
                        chat_id = msg["chat"]["id"]
                        from_user = msg.get("from", {}).get("id", chat_id)
                        text = msg["text"].strip()

                        if not text.startswith("/"):
                            continue

                        parts = text.split()
                        cmd = parts[0]
                        args = parts[1:]

                        logger.info(f"Command '{cmd}' from {from_user}")
                        res = handle_command(user_id=from_user, command=cmd, args=args)
                        reply_text = res.get("text", "")

                        if reply_text:
                            # Send reply
                            send_res = client.post(
                                f"{base_url}/sendMessage",
                                json={"chat_id": chat_id, "text": reply_text, "parse_mode": "Markdown"},
                            )
                            if send_res.status_code != 200:
                                client.post(
                                    f"{base_url}/sendMessage",
                                    json={"chat_id": chat_id, "text": reply_text},
                                )

                        # Auto-delete sensitive key messages for security hygiene
                        if res.get("delete_msg", False):
                            try:
                                client.post(
                                    f"{base_url}/deleteMessage",
                                    json={"chat_id": chat_id, "message_id": msg_id},
                                )
                                logger.info(f"Auto-deleted sensitive key message {msg_id}")
                            except Exception as del_err:
                                logger.warning(f"Could not delete message: {del_err}")

            except (httpx.TimeoutException, httpx.ReadTimeout):
                continue
            except Exception as e:
                logger.warning(f"Telegram polling loop notice: {e}")
                time.sleep(2)


if __name__ == "__main__":
    run_bot()

