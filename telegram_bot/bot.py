# LOCAL PRODUCTION PATCH — 2026-11-25 — alphaframehq/yt-Agent main c39f42541b8e784dcf41c09583dc57227be07667
# P0 fixes: #19 /script reads real DB video+audit or fails loudly (no hardcoded 13/13, PASS, 10:20, Stripe default);
#   #20 /run + run_* honour dispatch_pipeline_run() dispatched flag; #21 rej_/reg_/render_ do the real DB/dispatch
#   action first, edt_/thm_/shorts_ reply 'not implemented — no action taken'; #22 /videos + /preview say 'No videos
#   yet' / 'No preview available' (fake yt_priv_1292805d8f removed); #24 check_admin_auth fail-closed when
#   TELEGRAM_ADMIN_ID unset; #26 run_bot() sys.exit(1) on missing/invalid token or getMe failure.
#   P1: /ideas labelled curated starter examples, figures unverified; run_* resolves queue IDs via list_topics_queue.
# Local patch only — do NOT push to GitHub. All other code preserved verbatim from the pinned commit.
"""Telegram Admin Bot Control Plane for TubePilot AI.

Enforces strict security:
- Single-Admin authentication: Only TELEGRAM_ADMIN_ID can execute commands.
- Auto-deletes messages containing raw API keys for chat hygiene.
- Zero public webhooks: Coordinates with ephemeral runners via Google Sheets message bus.

Features & Modern UI:
- Interactive Visual Control Deck (/start, /menu, /help)
- AI Agent direct chat (grounded, anti-hallucination)
- 1-Tap Model Switcher (Gemini 2.0/2.5, DeepSeek V3/R1, Claude)
- Video Preview player & direct MP4/Shorts streaming
- Script Breakdown & 7 Deterministic Fact-Check Audit Inspector (/script)
- Instant Idea Brainstormer with 1-Tap Queue Buttons (/ideas)
- Video Production History (/videos)
- Topic Queue Management (/topic, /queue, /clear)
- Channel Branding & Like/Subscribe CTA Editor (/channel, /cta)
- 24/7 Cloud Runner & Codespaces Control (/codespace)
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
from db.sheets import (
    clear_topic_queue,
    enqueue_topic,
    get_latest_verification_log,
    get_video_record,
    list_recent_videos,
    list_topics_queue,
    update_video_status,
)
from telegram_bot.agent_chat import chat_with_agent, TRENDING_INSTITUTIONAL_TOPICS
from telegram_bot.codespace import handle_codespace_command
from telegram_bot.dispatch import dispatch_pipeline_run
from telegram_bot.health import get_cost_summary, get_pipeline_logs, get_system_ping
from telegram_bot.keys import handle_keys_command
from tools.secrets import _get_secret_optional, get_secret
from tools.youtube_api import publish_public

__all__ = [
    "handle_command",
    "handle_callback_query",
    "check_admin_auth",
    "run_bot",
]


def check_admin_auth(user_id: int | str) -> bool:
    """Enforce strict admin authorization check."""
    admin_id = _get_secret_optional("TELEGRAM_ADMIN_ID")
    if not admin_id:
        # P0 #24: fail closed. No admin configured => nobody is admin.
        # (Previously this returned True, letting anyone run every command.)
        logger.error("SECURITY: TELEGRAM_ADMIN_ID is not set — denying all Telegram commands (fail-closed).")
        return False
    return str(user_id) == str(admin_id)


def _build_keyboard(buttons: List[Dict[str, str]]) -> Dict[str, Any]:
    """Group buttons into clean 2-column mobile rows."""
    keyboard_rows = []
    current_row = []
    for btn in buttons:
        b_dict: Dict[str, str] = {"text": btn.get("text", "")}
        if "url" in btn:
            b_dict["url"] = btn["url"]
        elif "callback_data" in btn:
            b_dict["callback_data"] = btn["callback_data"]
        current_row.append(b_dict)
        if len(current_row) >= 2:
            keyboard_rows.append(current_row)
            current_row = []
    if current_row:
        keyboard_rows.append(current_row)
    return {"inline_keyboard": keyboard_rows}


def _handle_menu_command(args: Optional[List[str]] = None) -> Dict[str, Any]:
    """Render the Main Interactive Control Deck."""
    branding = get_channel_branding()
    models = get_config("active_models") or {}
    script_model = models.get("script", "gemini-3.8-flash").split("/")[-1]
    is_paused = bool(get_config("paused", False))
    status_emoji = "⏸ PAUSED" if is_paused else "▶️ ACTIVE"
    topics = list_topics_queue(status="pending")
    queue_count = len(topics)

    is_gh = os.getenv("GITHUB_ACTIONS") == "true"
    host_label = "☁️ GitHub Actions Cloud" if is_gh else "💻 Local / Codespace"

    text = (
        "🎬 *TubePilot AI — Control Deck*\n\n"
        f"📺 *Channel:* `{branding['channel_name']}`\n"
        f"🤖 *Engine:* `{script_model}`\n"
        f"⚙️ *Scheduler:* `{status_emoji}` | *Host:* `{host_label}`\n"
        f"📋 *Queue Depth:* `{queue_count} topic(s) pending`\n\n"
        "_Select an action from the dashboard or send any message to chat with your AI Agent:_"
    )

    buttons = [
        {"text": "🚀 Produce Video", "callback_data": "menu_run"},
        {"text": "💡 Brainstorm Ideas", "callback_data": "menu_ideas"},
        {"text": "🎬 Video Preview", "callback_data": "menu_preview"},
        {"text": "📝 Script & Audit", "callback_data": "menu_script"},
        {"text": "🤖 Switch Models", "callback_data": "menu_models"},
        {"text": "📋 Topic Queue", "callback_data": "menu_queue"},
        {"text": "📺 Channel & CTA", "callback_data": "menu_channel"},
        {"text": "🔑 API Keys & Pool", "callback_data": "menu_keys"},
        {"text": "📊 System Health", "callback_data": "menu_health"},
        {"text": "💻 Cloud Runners", "callback_data": "menu_runners"},
    ]

    return {"text": text, "buttons": buttons, "delete_msg": False}


def _handle_run_menu() -> Dict[str, Any]:
    """Submenu for launching video production."""
    text = (
        "🚀 *Dispatch Video Production*\n\n"
        "You can launch a pipeline run by typing:\n"
        "`/run <Topic Name>`\n\n"
        "Or tap one of our pre-researched institutional breakdowns below to start immediately:"
    )
    buttons = [
        {"text": "🚀 Run: Stripe Ledger", "callback_data": "run_stripe"},
        {"text": "🚀 Run: Plaid Bank Rail", "callback_data": "run_plaid"},
        {"text": "🚀 Run: Visa vs Mastercard", "callback_data": "run_visa"},
        {"text": "🚀 Run: Nubank P&L", "callback_data": "run_nubank"},
        {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
    ]
    return {"text": text, "buttons": buttons, "delete_msg": False}


def _handle_script_command(args: List[str]) -> Dict[str, Any]:
    """Subcommand handler for /script and fact-check verification inspection.

    P0 #19: reads the actual latest video record from Google Sheets (yt_videos)
    plus its latest stored verification audit (yt_verification_log) and
    reports ONLY what those records contain. No hardcoded audit, no
    hardcoded duration/scene count, no default "How Stripe Makes Money"
    topic. If no matching record/audit exists, says so loudly.
    """
    branding = get_channel_branding()
    channel_name = branding["channel_name"]
    cta = branding["cta"].get("like_subscribe", "")

    topic_filter = " ".join(args).strip() if args else ""

    try:
        vids = [v for v in list_recent_videos(limit=5) if v.get("video_id") != "yt_priv_1292805d8f"]
    except Exception as e:
        logger.error(f"/script: could not read video records: {e}")
        return {"text": f"❌ Could not read video records from Google Sheets: `{e}`", "delete_msg": False}

    if topic_filter:
        matched = [
            v for v in vids
            if topic_filter.lower() in str(v.get("title", "")).lower()
            or topic_filter.lower() in str(v.get("topic", "")).lower()
            or topic_filter == str(v.get("video_id", ""))
        ]
        if not matched:
            return {
                "text": (
                    f"❌ No script/audit found for `{topic_filter}`.\n\n"
                    "No video record matching that topic exists in yt_videos, and no "
                    "verification audit was found. Run `/run <topic>` to produce one first."
                ),
                "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}],
                "delete_msg": False,
            }
        video = matched[0]
    else:
        if not vids:
            return {
                "text": (
                    "❌ No script/audit found.\n\n"
                    "No video records exist in yt_videos yet — nothing has been produced, "
                    "so there is no script or fact-check audit to show. "
                    "Run `/run <topic>` to produce one first."
                ),
                "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}],
                "delete_msg": False,
            }
        video = vids[0]

    video_id = video.get("video_id", "unknown")
    title = video.get("title") or video.get("topic") or "Untitled"

    lines = [
        "📝 *Latest Video Script & Fact-Check Verification Audit*",
        "",
        f"• *Title:* `{title}`",
        f"• *Video ID:* `{video_id}`",
        f"• *Channel:* `{channel_name}`",
        f"• *Status:* `{video.get('status', 'unknown')}`",
    ]
    # Only show duration/cost when the record actually stores them.
    if video.get("duration") is not None:
        lines.append(f"• *Duration:* `{video.get('duration')}`")
    if video.get("cost_usd") is not None:
        lines.append(f"• *Recorded cost:* `${video.get('cost_usd')} USD`")
    lines.append("")
    lines.append("ℹ️ Script text is not stored in yt_videos, so the full script text cannot be shown here.")
    lines.append("")

    audit = None
    try:
        audit = get_latest_verification_log(video_id)
    except Exception as e:
        logger.error(f"/script: could not read verification log for {video_id}: {e}")

    if not audit:
        lines.append(
            "⚠️ *No verification audit found* for this video in yt_verification_log — "
            "no PASS/FAIL verdict is available. Do not treat this script as fact-checked."
        )
    else:
        lines.append("🔍 *Latest stored verification audit (yt_verification_log):*")
        lines.append(f"• *Iteration:* `{audit.get('iteration', 'unknown')}`")
        lines.append(f"• *Stored verdict:* `{audit.get('overall', 'unknown')}`")
        lines.append(f"• *Stored confidence:* `{audit.get('confidence', 'unknown')}`")
        lines.append(f"• *Model recorded:* `{audit.get('model_used', 'unknown')}`")
        # Surface real counts from the stored report, if it contains them.
        try:
            import json as _json
            report = _json.loads(audit.get("report_json") or "{}")
            if isinstance(report, dict):
                for key in ("critical_issues", "major_issues", "critical", "major", "claims_verified", "total_claims"):
                    if key in report:
                        lines.append(f"• *{key}:* `{report[key]}`")
        except Exception:
            pass
        lines.append("")
        lines.append("_Verdict above is exactly what is stored in the audit log; it was not re-verified here._")

    lines.append(f"📣 *CTA on file:* `{cta[:45]}...`" if cta else "📣 *CTA on file:* _none_")

    buttons = [
        {"text": "🎬 Watch Preview", "callback_data": "menu_preview"},
        {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
    ]

    return {"text": "\n".join(lines), "buttons": buttons, "delete_msg": False}


def _handle_ideas_command(args: List[str]) -> Dict[str, Any]:
    """Subcommand handler for /ideas and brainstorming."""
    lines = [
        "💡 *Trending Institutional FinTech Video Topics*",
        "_Curated starter examples only — figures in the hooks below (volumes, valuations, margins) are unverified "
        "and are not live research. Verify any figure before using it in a video._\n",
    ]

    buttons = []
    for idx, item in enumerate(TRENDING_INSTITUTIONAL_TOPICS[:4], 1):
        lines.append(f"*{idx}. {item['topic']}*")
        lines.append(f"• _Hook:_ \"{item['hook']}\"")
        lines.append(f"• _Pillar:_ `{item['pillar']}`\n")

        short_topic = item['topic'].split("(")[0].strip()
        buttons.append({"text": f"+ Queue #{idx} ({short_topic[:18]})", "callback_data": f"qidea_{idx}"})

    buttons.append({"text": "⬅️ Back to Menu", "callback_data": "menu_main"})

    return {"text": "\n".join(lines), "buttons": buttons, "delete_msg": False}


def _handle_videos_command(args: List[str]) -> Dict[str, Any]:
    """Subcommand handler for /videos."""
    # P0 #22: the known fake placeholder ID is never shown as a real video.
    vids = [v for v in list_recent_videos(limit=5) if v.get("video_id") != "yt_priv_1292805d8f"]
    lines = ["📺 *Recent Production Video History:*", ""]
    if not vids:
        lines.append("No videos yet — no production videos found in yt_videos.")
        lines.append("Use `/run <topic>` to produce your first video.")
    else:
        for v in vids:
            vid_id = v.get("video_id")
            title = v.get("title", "Video")
            status = v.get("status", "PRIVATE")
            lines.append(f"• `{vid_id}`: *{title}* ({status})")
            lines.append(f"  🔗 https://youtu.be/{vid_id}")

    buttons = [
        {"text": "🎬 View Latest Preview", "callback_data": "menu_preview"},
        {"text": "🚀 Produce New Video", "callback_data": "menu_run"},
        {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
    ]
    return {"text": "\n".join(lines), "buttons": buttons, "delete_msg": False}


def _handle_clear_command(args: List[str]) -> Dict[str, Any]:
    """Subcommand handler for /clear."""
    cleared = clear_topic_queue()
    return {
        "text": f"🗑 *Topic Queue Cleared!*\nRemoved `{cleared}` pending topics. Queue is now clean.",
        "delete_msg": False,
    }


def handle_command(user_id: int | str, command: str, args: List[str]) -> Dict[str, Any]:
    """Central router for all Telegram bot commands."""
    if not check_admin_auth(user_id):
        return {
            "text": "⛔️ *Unauthorized.* This bot is locked to the designated TubePilot administrator.",
            "delete_msg": False,
        }

    cmd = command.lower().strip().lstrip("/")

    # 0. /start, /help, /menu
    if cmd in ("start", "help", "menu"):
        return _handle_menu_command(args)

    # 1. /ping
    elif cmd == "ping":
        res = get_system_ping()
        buttons = [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}]
        return {"text": res["text"], "buttons": buttons, "delete_msg": False}

    # 2. /keys
    elif cmd == "keys":
        res = handle_keys_command(args)
        buttons = [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}]
        return {"text": res["text"], "buttons": buttons, "delete_msg": res.get("delete_caller_msg", False)}

    # 3. /model
    elif cmd == "model":
        return _handle_model_command(args)

    # 4. /chat, /ask, /agent
    elif cmd in ("chat", "ask", "agent"):
        prompt = " ".join(args).strip()
        reply = chat_with_agent(user_id=user_id, user_prompt=prompt)
        return {"text": reply, "delete_msg": False}

    # 5. /preview
    elif cmd in ("preview", "prev"):
        return _handle_preview_command(args, chat_id=user_id)

    # 6. /script, /audit, /factcheck
    elif cmd in ("script", "audit", "factcheck"):
        return _handle_script_command(args)

    # 7. /ideas, /brainstorm
    elif cmd in ("ideas", "brainstorm"):
        return _handle_ideas_command(args)

    # 8. /videos, /history
    elif cmd in ("videos", "history"):
        return _handle_videos_command(args)

    # 9. /topic, /queue
    elif cmd in ("topic", "queue"):
        return _handle_topic_command(args)

    # 10. /clear
    elif cmd in ("clear", "empty"):
        return _handle_clear_command(args)

    # 11. /run
    elif cmd == "run":
        if not args:
            return {"text": "❌ Usage: `/run <Topic or Company Name>`", "delete_msg": False}
        topic = " ".join(args)
        pillar = "tech_to_financial" if "tech" in topic.lower() else "finance_to_technical"
        dispatch_res = dispatch_pipeline_run(topic, pillar=pillar)
        # P0 #20: only claim a dispatch when dispatch_pipeline_run() says so.
        if not dispatch_res.get("dispatched"):
            return {
                "text": (
                    f"❌ Dispatch failed: {dispatch_res.get('details', 'unknown error')}\n\n"
                    f"• Topic: `{topic}`\n• Queue ID: `{dispatch_res.get('topic_id')}`\n\n"
                    "The topic is queued in Google Sheets, but no GitHub Actions run was started."
                ),
                "buttons": [
                    {"text": "📋 Topic Queue", "callback_data": "menu_queue"},
                    {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
                ],
                "delete_msg": False,
            }
        return {
            "text": f"🚀 *Pipeline Dispatched!*\n\n• Topic: `{topic}`\n• Queue ID: `{dispatch_res['topic_id']}`\n• Status: {dispatch_res['details']}",
            "buttons": [
                {"text": "📋 Topic Queue", "callback_data": "menu_queue"},
                {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
            ],
            "delete_msg": False,
        }

    # 12. /draft (Script + Fact-Check without rendering yet)
    elif cmd == "draft":
        if not args:
            return {"text": "❌ Usage: `/draft <Topic Name>`", "delete_msg": False}
        return _handle_script_command(args)

    # 13. /status
    elif cmd == "status":
        topics = list_topics_queue()
        pending = sum(1 for t in topics if t.get("status") == "pending")
        running = sum(1 for t in topics if t.get("status") == "running")
        return {
            "text": f"📊 *Queue Status:*\n• Running pipelines: `{running}`\n• Pending in queue: `{pending}`\n• Total tracked: `{len(topics)}`",
            "buttons": [
                {"text": "🚀 Produce Video", "callback_data": "menu_run"},
                {"text": "📋 View Queue", "callback_data": "menu_queue"},
                {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
            ],
            "delete_msg": False,
        }

    # 14. /pause and /resume
    elif cmd in ("pause", "resume"):
        new_status = (cmd == "pause")
        set_config("paused", new_status)
        return {
            "text": f"{'⏸ Pipeline paused.' if new_status else '▶️ Pipeline resumed.'} Autonomous cron schedule updated.",
            "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}],
            "delete_msg": False,
        }

    # 15. /config
    elif cmd == "config":
        return _handle_config_command(args)

    # 16. /channel or /channelname
    elif cmd in ("channel", "channelname"):
        return _handle_channel_command(args)

    # 17. /cta
    elif cmd == "cta":
        return _handle_cta_command(args)

    # 18. /cost
    elif cmd == "cost":
        vid = args[0] if args else ""
        return {"text": get_cost_summary(vid), "delete_msg": False}

    # 19. /logs
    elif cmd == "logs":
        rid = args[0] if args else ""
        return {"text": get_pipeline_logs(rid), "delete_msg": False}

    # 20. /codespace
    elif cmd == "codespace":
        return {"text": handle_codespace_command(args), "delete_msg": False}

    # Fallback to main menu
    return _handle_menu_command(args)


def handle_callback_query(callback_data: str) -> Dict[str, Any]:
    """Handle interactive inline buttons across all submenus and approval workflows."""
    logger.info(f"Telegram callback query received: {callback_data}")

    # Navigation callbacks
    if callback_data == "menu_main":
        return _handle_menu_command([])
    elif callback_data == "menu_run":
        return _handle_run_menu()
    elif callback_data == "menu_ideas":
        return _handle_ideas_command([])
    elif callback_data == "menu_preview":
        return _handle_preview_command([])
    elif callback_data == "menu_script":
        return _handle_script_command([])
    elif callback_data == "menu_models":
        return _handle_model_command([])
    elif callback_data == "menu_queue":
        return _handle_topic_command([])
    elif callback_data == "menu_channel":
        return _handle_channel_command([])
    elif callback_data == "menu_keys":
        return handle_keys_command([])
    elif callback_data == "menu_health":
        res = get_system_ping()
        return {"text": res["text"], "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}]}
    elif callback_data == "menu_runners":
        return {"text": handle_codespace_command(["list"]), "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}]}
    elif callback_data == "action_clear_queue":
        return _handle_clear_command([])

    # Fast 1-tap topic queueing from ideas
    elif callback_data.startswith("qidea_"):
        idx = int(callback_data[6:]) - 1
        if 0 <= idx < len(TRENDING_INSTITUTIONAL_TOPICS):
            item = TRENDING_INSTITUTIONAL_TOPICS[idx]
            top_id = enqueue_topic(item["topic"], pillar=item["pillar"], priority=5)
            return {
                "text": f"✅ *Topic Added to Queue!*\n\n• Topic: `{item['topic']}`\n• Priority: `High`\n• ID: `{top_id}`\n\nTap `/queue` to inspect upcoming topics.",
                "buttons": [
                    {"text": "📋 View Queue", "callback_data": "menu_queue"},
                    {"text": "🚀 Launch Now", "callback_data": f"run_{top_id}"},
                    {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
                ],
            }

    # Fast 1-tap run triggers
    elif callback_data.startswith("run_"):
        raw_id = callback_data[4:]
        # P1 queue-ID bug: callbacks such as run_top_<md5> (emitted by
        # qidea_ / /topic add "Launch Now" buttons) carry a QUEUE ID, not a
        # topic name. Resolve it against the queue first; never dispatch
        # the raw ID as a garbled topic.
        topic = None
        pillar = "finance_to_technical"
        try:
            queued = [t for t in list_topics_queue() if str(t.get("topic_id")) == raw_id]
        except Exception as e:
            logger.error(f"run_ callback: could not read topic queue: {e}")
            queued = []
        if queued:
            topic = queued[0].get("topic")
            pillar = queued[0].get("pillar") or pillar
        elif raw_id in ("stripe", "plaid", "visa", "nubank"):
            topic = {
                "stripe": "How Stripe Makes Money",
                "plaid": "How Plaid Connects Banks",
                "visa": "Visa vs Mastercard Payment Rails",
                "nubank": "Nubank Unit Economics & P&L",
            }[raw_id]
        elif raw_id.startswith("top_"):
            return {
                "text": f"❌ Queue ID `{raw_id}` not found — no run dispatched.",
                "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}],
            }
        else:
            # Legacy topic-slug button (e.g. from an old /script message).
            topic = raw_id.replace("_", " ").title()
            pillar = "tech_to_financial" if "tech" in topic.lower() else "finance_to_technical"

        dispatch_res = dispatch_pipeline_run(topic, pillar=pillar)
        # P0 #20: only claim a dispatch when dispatch_pipeline_run() says so.
        if not dispatch_res.get("dispatched"):
            return {
                "text": (
                    f"❌ Dispatch failed: {dispatch_res.get('details', 'unknown error')}\n\n"
                    f"• Topic: `{topic}`\n• Queue ID: `{dispatch_res.get('topic_id')}`"
                ),
                "buttons": [
                    {"text": "📋 View Queue", "callback_data": "menu_queue"},
                    {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
                ],
            }
        return {
            "text": f"🚀 *Pipeline Dispatched!*\n\n• Topic: `{topic}`\n• ID: `{dispatch_res['topic_id']}`\n• Status: {dispatch_res['details']}",
            "buttons": [
                {"text": "📋 View Queue", "callback_data": "menu_queue"},
                {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
            ],
        }

    # Publishing & Review Approvals
    elif callback_data.startswith("pub_"):
        yt_id = callback_data[4:]
        ok = publish_public(yt_id)
        if ok:
            return {"text": f"🎉 *VIDEO PUBLISHED PUBLIC!*\n\nYouTube ID: `{yt_id}`\nURL: https://youtu.be/{yt_id}"}
        return {"text": f"❌ Could not publish video `{yt_id}`. Please check YouTube API status."}

    elif callback_data.startswith("rej_"):
        # P0 #21: reject only after the real DB update succeeds.
        yt_id = callback_data[4:]
        try:
            rec = get_video_record(yt_id)
        except Exception as e:
            return {"text": f"❌ Could not reject video `{yt_id}`: could not read its record ({e}) — no action taken."}
        if rec is None:
            return {"text": f"❌ Video `{yt_id}` not found in yt_videos — no action taken."}
        try:
            ok = update_video_status(yt_id, "rejected")
        except Exception as e:
            return {"text": f"❌ Could not reject video `{yt_id}`: `{e}` — no action taken."}
        if not ok:
            return {"text": f"❌ Could not reject video `{yt_id}`: status update failed — no action taken."}
        return {"text": f"❌ Video `{yt_id}` marked rejected in yt_videos.\n\n_Note: this only updates the DB record; it does not delete the video from YouTube._"}

    elif callback_data.startswith("reg_"):
        # P0 #21: re-enqueue only after the real enqueue succeeds.
        v_id = callback_data[4:]
        try:
            rec = get_video_record(v_id)
        except Exception as e:
            return {"text": f"❌ Could not re-enqueue video `{v_id}`: could not read its record ({e}) — no action taken."}
        if rec is None:
            return {"text": f"❌ Video `{v_id}` not found in yt_videos — no action taken."}
        topic = rec.get("topic") or rec.get("title")
        if not topic:
            return {"text": f"❌ Not implemented — no action taken: video `{v_id}` has no topic/title on its record to re-enqueue."}
        try:
            top_id = enqueue_topic(topic, pillar=rec.get("pillar") or "finance_to_technical", priority=10)
        except Exception as e:
            return {"text": f"❌ Could not re-enqueue video `{v_id}`: `{e}` — no action taken."}
        return {"text": f"🔁 Video `{v_id}` re-enqueued for regeneration.\n\n• Topic: `{topic}`\n• Queue ID: `{top_id}`"}

    elif callback_data.startswith("render_"):
        # P0 #21: /preview offers a Re-Render button; dispatch a real run
        # for the record's topic and confirm only if dispatch succeeds.
        v_id = callback_data[7:]
        try:
            rec = get_video_record(v_id)
        except Exception as e:
            return {"text": f"❌ Could not re-render video `{v_id}`: could not read its record ({e}) — no action taken."}
        if rec is None:
            return {"text": f"❌ Video `{v_id}` not found in yt_videos — no action taken."}
        topic = rec.get("topic") or rec.get("title")
        if not topic:
            return {"text": f"❌ Not implemented — no action taken: video `{v_id}` has no topic/title on its record to re-render."}
        dispatch_res = dispatch_pipeline_run(topic, pillar=rec.get("pillar") or "finance_to_technical")
        if not dispatch_res.get("dispatched"):
            return {"text": f"❌ Dispatch failed: {dispatch_res.get('details', 'unknown error')}\n\n• Topic: `{topic}` — no re-render started."}
        return {"text": f"🔄 Re-render dispatched for `{v_id}`.\n\n• Topic: `{topic}`\n• Queue ID: `{dispatch_res.get('topic_id')}`"}

    elif callback_data.startswith("edt_"):
        # P0 #21: no edit workflow is wired in this bot — there is no stored
        # pending-edit state and no handler for a reply with a new title, so
        # claiming an edit would be fake. Say so instead of claiming success.
        return {"text": "❌ Not implemented — no action taken: title/description editing is not wired up in this bot (no edit is stored or applied)."}

    elif callback_data.startswith("thm_"):
        # P0 #21: no thumbnail-replacement workflow is wired in this bot.
        return {"text": "❌ Not implemented — no action taken: custom thumbnail replacement is not wired up in this bot (no image is stored or applied)."}

    elif callback_data.startswith("mdl_"):
        target_mdl = callback_data[4:]
        if target_mdl == "reset":
            reset_config()
            return {
                "text": "🔄 Model routing reset to default Gemini 3.8 Flash configuration.",
                "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}],
            }
        res = _handle_model_command([target_mdl])
        res["buttons"] = [
            {"text": "🤖 AI Models", "callback_data": "menu_models"},
            {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
        ]
        return res

    elif callback_data.startswith("prv_"):
        vid = callback_data[4:]
        return {
            "text": f"🎬 *Video Preview Card*\n\nURL: https://youtu.be/{vid}",
            "buttons": [
                {"text": "🎬 Watch Player Preview", "url": f"https://youtu.be/{vid}"},
                {"text": "🚀 Publish Public", "callback_data": f"pub_{vid}"},
                {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
            ],
        }

    elif callback_data.startswith("shorts_"):
        # P0 #21: no Shorts generation/scheduling is called from this bot.
        vid = callback_data[7:]
        return {"text": f"❌ Not implemented — no action taken: no Shorts were prepared or scheduled for `{vid}` (no Shorts workflow is called from this bot)."}

    return {"text": "Action processed.", "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}]}


def _handle_model_command(args: List[str]) -> Dict[str, Any]:
    """Subcommand handler for /model."""
    models = get_config("active_models") or {
        "research": "gemini/gemini-3.8-flash",
        "script": "gemini/gemini-3.8-flash",
        "verify": "gemini/gemini-3.8-flash",
        "image": "gemini/imagen-3.0-generate-001",
    }

    if not args or args[0].lower() in ("list", "status", "show"):
        lines = [
            "🤖 *Active AI Model Routing:*",
            f"• *Research:* `{models.get('research', 'gemini/gemini-3.8-flash')}`",
            f"• *Script:* `{models.get('script', 'gemini/gemini-3.8-flash')}`",
            f"• *Verify:* `{models.get('verify', 'gemini/gemini-3.8-flash')}`",
            f"• *Image:* `{models.get('image', 'gemini/imagen-3.0-generate-001')}`",
            "",
            "✨ *Quick Switch Commands:*",
            "• `/model gemini-3.8` — Set all text models to Gemini 3.8 Flash",
            "• `/model gemini-3.5-lite` — Set all text models to Gemini 3.5 Flash Lite",
            "• `/model free` — Set all text models to the free OpenRouter model",
            "• `/model script free` — Set only script model",
            "• `/model verify gemini-2.0` — Set only verification model",
            "• `/model reset` — Reset to default configuration",
        ]
        buttons = [
            {"text": "⚡️ Gemini 3.8", "callback_data": "mdl_gemini-3.8"},
            {"text": "🚀 Gemini 3.5 Lite", "callback_data": "mdl_gemini-3.5-lite"},
            {"text": "🆓 Free Model", "callback_data": "mdl_free"},
            {"text": "🔄 Reset Defaults", "callback_data": "mdl_reset"},
            {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
        ]
        return {"text": "\n".join(lines), "buttons": buttons, "delete_msg": False}

    sub = args[0].lower()
    if sub == "reset":
        reset_config()
        return {
            "text": "🔄 Model routing reset to defaults (Gemini 3.8 Flash + Gemini Multi-Key Failover Pool).",
            "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}],
            "delete_msg": False,
        }

    def resolve_model(target: str) -> str:
        t = target.lower().strip()
        if t in ("gemini", "gemini-3.8", "gemini-3.8-flash", "gemini-2.0", "gemini-2", "flash", "gemini-flash"):
            return "gemini/gemini-3.8-flash"
        elif t in ("gemini-3.5-lite", "gemini-3.5-flash-lite", "gemini-2.5", "gemini-2.5-lite", "lite", "flash-lite"):
            return "gemini/gemini-3.5-flash-lite"
        elif t in ("free", "openrouter-free", "deepseek", "deepseek-chat", "deepseek-v3", "v3", "deepseek-r1", "r1", "reasoner"):
            # No free DeepSeek model exists on OpenRouter anymore
            # (deepseek/deepseek-chat:free is gone; deepseek-chat is paid),
            # so free-model requests route via OpenRouter's free router.
            return "openrouter/openrouter/free"
        elif t in ("claude", "haiku", "claude-3.5", "bedrock"):
            return "openrouter/anthropic/claude-3.5-haiku"
        elif t in ("imagen", "imagen-3", "imagen3"):
            return "gemini/imagen-3.0-generate-001"
        elif t in ("flux", "flux-pro"):
            return "replicate/flux-1.1-pro"
        elif t in ("sdxl", "fast-sdxl"):
            return "fal/fast-sdxl"
        return target

    if sub in ("switch", "set"):
        args = args[1:]
        if not args:
            return {"text": "❌ Usage: `/model switch <model>` or `/model switch <role> <model>`", "delete_msg": False}
        sub = args[0].lower()

    ROLE_MAP = {
        "script": "script",
        "writer": "script",
        "verify": "verify",
        "verification": "verify",
        "factcheck": "verify",
        "research": "research",
        "researcher": "research",
        "image": "image",
        "visual": "image",
        "thumbnail": "image",
        "thumb": "image",
    }

    if len(args) == 1:
        if sub in ROLE_MAP:
            return {"text": f"❌ Usage: `/model {sub} <model_name>`", "delete_msg": False}
        resolved = resolve_model(sub)
        set_config("active_models.research", resolved)
        set_config("active_models.script", resolved)
        set_config("active_models.verify", resolved)
        return {
            "text": (
                f"✅ *All Text Models Switched Globally!*\n\n"
                f"• Target Model: `{resolved}`\n"
                f"• Applied to: `research`, `script`, `verify`\n\n"
                "All future pipeline runs will use this model."
            ),
            "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}],
            "delete_msg": False,
        }

    role_arg = sub
    model_arg = args[1]

    if role_arg in ("all", "global", "text"):
        resolved = resolve_model(model_arg)
        set_config("active_models.research", resolved)
        set_config("active_models.script", resolved)
        set_config("active_models.verify", resolved)
        return {
            "text": f"✅ All text pipeline stages (`research`, `script`, `verify`) switched to `{resolved}`.",
            "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}],
            "delete_msg": False,
        }

    canonical_role = ROLE_MAP.get(role_arg, role_arg)
    resolved = resolve_model(model_arg)
    set_config(f"active_models.{canonical_role}", resolved)

    return {
        "text": f"✅ Model for `{canonical_role}` set to `{resolved}`. Takes effect on next pipeline run.",
        "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}],
        "delete_msg": False,
    }


def _handle_preview_command(args: List[str], chat_id: Optional[int | str] = None) -> Dict[str, Any]:
    """Subcommand handler for /preview."""
    renders_dir = Path(_ROOT) / "cache" / "renders"
    images_dir = Path(_ROOT) / "cache" / "images"
    charts_dir = Path(_ROOT) / "cache" / "charts"

    video_files = list(renders_dir.glob("*.mp4")) if renders_dir.exists() else []
    shorts_files = [f for f in video_files if "short" in f.name.lower()]
    long_files = [f for f in video_files if "long" in f.name.lower() or "short" not in f.name.lower()]

    sub = args[0].lower() if args else ""
    target_vid: Optional[Path] = None

    if sub in ("shorts", "short") and shorts_files:
        target_vid = shorts_files[0]
    elif sub in ("long", "longform") and long_files:
        target_vid = long_files[0]
    elif video_files:
        target_vid = shorts_files[0] if shorts_files else video_files[0]

    if target_vid and target_vid.is_file():
        from scripts.notify import send_telegram_video
        size_mb = target_vid.stat().st_size / (1024 * 1024)
        caption = f"🎬 *Video Preview Clip:* `{target_vid.name}` ({size_mb:.2f} MB)"
        sent = send_telegram_video(str(target_vid), caption=caption, chat_id=chat_id)
        if sent:
            return {
                "text": f"✅ Delivered local video preview: `{target_vid.name}` directly to this chat.",
                "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}],
                "delete_msg": False,
            }

    recent_images = (list(images_dir.glob("*.png")) + list(charts_dir.glob("*.png"))) if images_dir.exists() else []
    if recent_images and sub in ("image", "thumb", "thumbnail", "chart"):
        from scripts.notify import send_telegram_photo
        img_target = recent_images[-1]
        sent = send_telegram_photo(str(img_target), caption=f"🖼 Visual Asset: `{img_target.name}`", chat_id=chat_id)
        if sent:
            return {
                "text": f"✅ Delivered visual preview: `{img_target.name}`.",
                "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}],
                "delete_msg": False,
            }

    # P0 #22: no fake fallback video. Only a real yt_videos record (or a
    # user-supplied ID that exists in yt_videos) may be previewed/published.
    yt_id = None
    if args and not args[0].startswith("-") and args[0] not in ("shorts", "long", "image", "thumb", "thumbnail", "chart"):
        candidate = args[0]
        try:
            yt_id = candidate if get_video_record(candidate) else None
        except Exception:
            yt_id = None
        if yt_id is None:
            return {
                "text": f"🎬 No preview available for `{candidate}` — no such video record found in yt_videos.",
                "buttons": [
                    {"text": "🚀 Produce New Video", "callback_data": "menu_run"},
                    {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
                ],
                "delete_msg": False,
            }
    else:
        try:
            recent = [v for v in list_recent_videos(limit=5) if v.get("video_id") != "yt_priv_1292805d8f"]
        except Exception:
            recent = []
        if not recent:
            return {
                "text": "🎬 No preview available — no videos produced yet.\n\nUse `/run <topic>` to produce your first video.",
                "buttons": [
                    {"text": "🚀 Produce New Video", "callback_data": "menu_run"},
                    {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
                ],
                "delete_msg": False,
            }
        yt_id = recent[0].get("video_id")
    yt_url = f"https://youtu.be/{yt_id}"
    branding = get_channel_branding()

    return {
        "text": (
            "🎬 *Video Production Preview*\n\n"
            f"• *Channel:* `{branding['channel_name']}`\n"
            f"• *Status:* 🔒 Uploaded as PRIVATE\n"
            f"• *YouTube ID:* `{yt_id}`\n\n"
            f"🔗 [Watch Private Preview Player]({yt_url})\n\n"
            "_Tap an action below to publish publicly or re-render:_"
        ),
        "buttons": [
            {"text": "🎬 Watch Player Preview", "url": yt_url},
            {"text": "🚀 Publish Public", "callback_data": f"pub_{yt_id}"},
            {"text": "🗑️ Reject & Delete", "callback_data": f"rej_{yt_id}"},
            {"text": "🔄 Re-Render", "callback_data": f"render_{yt_id}"},
            {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
        ],
        "delete_msg": False,
    }


def _handle_topic_command(args: List[str]) -> Dict[str, Any]:
    """Subcommand handler for /topic and /queue."""
    if not args or args[0].lower() == "list":
        topics = list_topics_queue()
        if not topics:
            return {
                "text": "📋 *Topic Queue is currently empty.*\n\nAdd a topic using `/topic add <Title>` or tap below for 1-tap suggestions:",
                "buttons": [
                    {"text": "💡 Brainstorm Ideas", "callback_data": "menu_ideas"},
                    {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
                ],
                "delete_msg": False,
            }
        lines = ["📋 *Upcoming Topics in Queue:*"]
        for t in topics[:8]:
            lines.append(f"• `[{t.get('status').upper()}]` {t.get('topic')} ({t.get('pillar')})")

        buttons = [
            {"text": "💡 Brainstorm Ideas", "callback_data": "menu_ideas"},
            {"text": "🗑 Clear Queue", "callback_data": "action_clear_queue"},
            {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
        ]
        return {"text": "\n".join(lines), "buttons": buttons, "delete_msg": False}

    sub = args[0].lower()
    if sub == "add":
        if len(args) < 2:
            return {"text": "❌ Usage: `/topic add <Title> [finance|tech]`", "delete_msg": False}
        pillar = "tech_to_financial" if "tech" in args[-1].lower() else "finance_to_technical"
        title_words = args[1:-1] if args[-1].lower() in ("finance", "tech") else args[1:]
        topic_title = " ".join(title_words).strip('"\'')

        top_id = enqueue_topic(topic_title, pillar=pillar)
        return {
            "text": f"✅ Topic added to queue!\n• Title: `{topic_title}`\n• Pillar: `{pillar}`\n• ID: `{top_id}`",
            "buttons": [
                {"text": "📋 View Queue", "callback_data": "menu_queue"},
                {"text": "🚀 Launch Now", "callback_data": f"run_{top_id}"},
                {"text": "⬅️ Back to Menu", "callback_data": "menu_main"},
            ],
            "delete_msg": False,
        }

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
            "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}],
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

        if raw_val.lower() in ("true", "false"):
            val_parsed: Any = (raw_val.lower() == "true")
        else:
            try:
                val_parsed = float(raw_val) if "." in raw_val else int(raw_val)
            except ValueError:
                val_parsed = raw_val

        set_config(key, val_parsed)
        return {
            "text": f"✅ Configuration updated: `{key}` = `{val_parsed}`",
            "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}],
            "delete_msg": False,
        }

    return {"text": "Usage: `/config` or `/config set <key> <val>`", "delete_msg": False}


def _handle_channel_command(args: List[str]) -> Dict[str, Any]:
    """Subcommand handler for /channel."""
    if not args:
        branding = get_channel_branding()
        return {
            "text": (
                f"📺 *Current YouTube Channel Name:* `{branding['channel_name']}`\n\n"
                "To update, use:\n`/channel <New Channel Name>`"
            ),
            "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}],
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
            "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}],
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
            "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}],
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
            "buttons": [{"text": "⬅️ Back to Menu", "callback_data": "menu_main"}],
            "delete_msg": False,
        }
    except Exception as e:
        return {"text": f"❌ Error saving CTA: {e}", "delete_msg": False}


def _retrigger_github_actions_bot() -> bool:
    """Dispatches the next GitHub Actions bot runner workflow before runner timeout."""
    gh_pat = _get_secret_optional("GH_PAT") or os.getenv("GITHUB_TOKEN")
    full_repo = os.getenv("GITHUB_REPOSITORY", "")
    if "/" in full_repo:
        owner, repo = full_repo.split("/", 1)
    else:
        owner = os.getenv("GH_REPO_OWNER", "alphaframehq")
        repo = os.getenv("GH_REPO_NAME", "yt-Agent")

    if not gh_pat:
        logger.warning("Cannot retrigger bot workflow: GH_PAT/GITHUB_TOKEN missing.")
        return False

    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/bot.yml/dispatches"
    try:
        import httpx
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                url,
                headers={"Authorization": f"Bearer {gh_pat}", "Accept": "application/vnd.github.v3+json"},
                json={"ref": "main"},
            )
            if resp.status_code == 204:
                logger.info("✅ Successfully dispatched next continuous GitHub Actions bot runner!")
                return True
            else:
                logger.warning(f"Could not dispatch bot workflow ({resp.status_code}): {resp.text}")
                return False
    except Exception as e:
        logger.error(f"Error dispatching next bot workflow: {e}")
        return False


def run_bot(max_duration_seconds: Optional[int] = None) -> None:
    """Run Telegram bot listener in continuous long-polling mode."""
    import time
    import httpx

    try:
        from dotenv import load_dotenv
        env_file = Path(__file__).resolve().parent.parent / ".env"
        if env_file.exists():
            load_dotenv(env_file)
    except ImportError:
        pass

    try:
        token = get_secret("TELEGRAM_BOT_TOKEN")
    except RuntimeError:
        # P0 #26: exit non-zero so the workflow/runner knows startup failed.
        logger.error("TELEGRAM_BOT_TOKEN is not configured. Cannot start Telegram bot.")
        sys.exit(1)

    admin_id = _get_secret_optional("TELEGRAM_ADMIN_ID")
    if not admin_id:
        logger.error("TELEGRAM_ADMIN_ID is not set — check_admin_auth() will deny all commands (fail-closed).")
    is_gh_actions = os.getenv("GITHUB_ACTIONS") == "true"
    run_id = os.getenv("GITHUB_RUN_ID", "local")
    logger.info(f"Starting Telegram bot listener (Admin: {admin_id}, GH Actions: {is_gh_actions}, Run: {run_id})")

    base_url = f"https://api.telegram.org/bot{token}"
    offset = 0
    start_time = time.time()

    with httpx.Client(timeout=35.0) as client:
        try:
            me = client.get(f"{base_url}/getMe").json()
            if me.get("ok"):
                username = me["result"].get("username", "bot")
                first_name = me["result"].get("first_name", "")
                logger.info(f"✅ Bot connected to Telegram as @{username} ({first_name}). Awaiting commands...")
            else:
                # P0 #26: invalid token / getMe failure must not exit 0.
                logger.error(f"Failed to authenticate bot: {me}")
                sys.exit(1)
        except Exception as e:
            logger.error(f"Network error connecting to Telegram: {e}")
            sys.exit(1)

        while True:
            if max_duration_seconds and (time.time() - start_time) >= max_duration_seconds:
                logger.info(f"Cloud runner duration limit reached ({max_duration_seconds}s). Handing over...")
                _retrigger_github_actions_bot()
                break

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

                    # 1. Handle Callback Queries
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
                            json={"callback_query_id": cb_id, "text": "Action processed."},
                        )
                        if chat_id and res.get("text"):
                            send_payload = {
                                "chat_id": chat_id,
                                "text": res["text"],
                                "parse_mode": "Markdown",
                            }
                            if res.get("buttons"):
                                send_payload["reply_markup"] = _build_keyboard(res["buttons"])
                            send_res = client.post(f"{base_url}/sendMessage", json=send_payload)
                            if send_res.status_code != 200:
                                send_payload.pop("parse_mode", None)
                                client.post(f"{base_url}/sendMessage", json=send_payload)
                        continue

                    # 2. Handle Text Messages
                    if "message" in upd and "text" in upd["message"]:
                        msg = upd["message"]
                        msg_id = msg["message_id"]
                        chat_id = msg["chat"]["id"]
                        from_user = msg.get("from", {}).get("id", chat_id)
                        raw_text = msg["text"].strip()

                        if not raw_text:
                            continue

                        if raw_text.startswith("/"):
                            parts = raw_text.split()
                            cmd = parts[0]
                            args = parts[1:]

                            logger.info(f"Command '{cmd}' from {from_user}")
                            res = handle_command(user_id=from_user, command=cmd, args=args)
                            reply_text = res.get("text", "")
                            buttons = res.get("buttons")

                            if reply_text:
                                send_payload = {
                                    "chat_id": chat_id,
                                    "text": reply_text,
                                    "parse_mode": "Markdown",
                                }
                                if buttons:
                                    send_payload["reply_markup"] = _build_keyboard(buttons)

                                send_res = client.post(f"{base_url}/sendMessage", json=send_payload)
                                if send_res.status_code != 200:
                                    send_payload.pop("parse_mode", None)
                                    client.post(f"{base_url}/sendMessage", json=send_payload)

                            if res.get("delete_msg", False):
                                try:
                                    client.post(
                                        f"{base_url}/deleteMessage",
                                        json={"chat_id": chat_id, "message_id": msg_id},
                                    )
                                except Exception as del_err:
                                    logger.warning(f"Could not delete message: {del_err}")

                        else:
                            if not check_admin_auth(from_user):
                                client.post(
                                    f"{base_url}/sendMessage",
                                    json={
                                        "chat_id": chat_id,
                                        "text": "⛔️ *Unauthorized.* This bot is locked to the designated TubePilot administrator.",
                                        "parse_mode": "Markdown",
                                    },
                                )
                                continue

                            logger.info(f"Direct conversation with AI Agent from {from_user}: {raw_text[:50]}")
                            try:
                                client.post(f"{base_url}/sendChatAction", json={"chat_id": chat_id, "action": "typing"})
                            except Exception:
                                pass

                            agent_reply = chat_with_agent(user_id=from_user, user_prompt=raw_text)
                            send_payload = {
                                "chat_id": chat_id,
                                "text": agent_reply,
                                "parse_mode": "Markdown",
                                "reply_markup": _build_keyboard([
                                    {"text": "🚀 Produce Video", "callback_data": "menu_run"},
                                    {"text": "📋 Dashboard Menu", "callback_data": "menu_main"},
                                ]),
                            }
                            send_res = client.post(f"{base_url}/sendMessage", json=send_payload)
                            if send_res.status_code != 200:
                                send_payload.pop("parse_mode", None)
                                client.post(f"{base_url}/sendMessage", json=send_payload)

            except (httpx.TimeoutException, httpx.ReadTimeout):
                continue
            except Exception as e:
                logger.warning(f"Telegram polling loop notice: {e}")
                time.sleep(2)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TubePilot Telegram Bot")
    parser.add_argument("--run-duration", type=int, default=0, help="Max seconds to run before cleanly exiting (0 for infinite)")
    parsed_args, _ = parser.parse_known_args()
    dur = parsed_args.run_duration if parsed_args.run_duration > 0 else None
    run_bot(max_duration_seconds=dur)
