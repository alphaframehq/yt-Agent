# LOCAL PRODUCTION PATCH — 2026-11-25 — alphaframehq/yt-Agent main c39f42541b8e784dcf41c09583dc57227be07667
# P0 fix (#23): _build_grounding_context() omits the video section's fake entry and states 'No videos produced
#   yet' when list_recent_videos() is empty. Removed injected fake yt_priv_1292805d8f 'REAL VERIFIED SYSTEM
#   CONTEXT' Stripe video (also filtered out if that ID is present in the DB).
# Local patch only — do NOT push to GitHub.
"""Agent Chat Assistant for TubePilot AI Telegram Control Plane.

Provides grounded, anti-hallucination conversational intelligence for the YouTube channel owner.
Seamlessly handles domain analysis, video scripting inquiries, fact-check audit reports,
topic queueing, and direct action dispatch.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional
from loguru import logger

from config_manager import get_channel_branding, get_config, set_config
from db.sheets import (
    clear_topic_queue,
    enqueue_topic,
    get_video_record,
    list_recent_videos,
    list_topics_queue,
)
from telegram_bot.dispatch import dispatch_pipeline_run
from telegram_bot.health import get_system_ping
from tools.llm_provider import call_with_fallback
from tools.secrets import get_secret

__all__ = ["chat_with_agent"]

# Curated institutional topics for instant suggestions
TRENDING_INSTITUTIONAL_TOPICS = [
    {
        "topic": "How Stripe Makes Money (Interchange & Treasury Engine)",
        "pillar": "finance_to_technical",
        "hook": "Every second, Stripe processes thousands of transactions without touching a single physical vault. Here is the hidden P&L architecture behind their $1 trillion volume.",
    },
    {
        "topic": "How Revolut Generates Millions Despite European Interchange Caps",
        "pillar": "finance_to_technical",
        "hook": "When the EU capped debit interchange at 0.2%, European fintechs were supposed to die. Here is how Revolut engineered a $45B valuation anyway.",
    },
    {
        "topic": "Why Cloudflare's Anycast Edge Network Cannot Be Cloned",
        "pillar": "tech_to_financial",
        "hook": "Cloudflare routes 20% of global internet traffic through servers sitting in 330 cities. Here is the BGP Anycast protocol and unit economics behind it.",
    },
    {
        "topic": "How Wise Moves $100 Billion Without Crossing International Borders",
        "pillar": "finance_to_technical",
        "hook": "Traditional banks use SWIFT and charge 3%. Wise uses zero cross-border wires. Here is the local net settlement ledger that changed foreign exchange forever.",
    },
    {
        "topic": "The Real Architecture Behind Adyen's 50% EBITDA Margin",
        "pillar": "finance_to_technical",
        "hook": "Unlike legacy acquirers with 20 fragmented platforms, Adyen built one single codebase. Here is how full-stack processing prints a 50% cash margin.",
    },
]


def _build_grounding_context() -> str:
    """Collect real-time factual state to eliminate LLM hallucinations."""
    branding = get_channel_branding()
    models = get_config("active_models") or {
        "research": "gemini/gemini-2.0-flash",
        "script": "gemini/gemini-2.0-flash",
        "verify": "gemini/gemini-2.0-flash",
        "image": "gemini/imagen-3.0-generate-001",
    }
    budget = get_config("budget_ai_per_video", 0.45)
    paused = bool(get_config("paused", False))

    # Real topics
    topics = list_topics_queue()
    pending = [t.get("topic") for t in topics if t.get("status") == "pending"]
    running = [t.get("topic") for t in topics if t.get("status") == "running"]

    # Real videos only — the known fake placeholder ID is never injected.
    recent_vids = [v for v in list_recent_videos(limit=3) if v.get("video_id") != "yt_priv_1292805d8f"]
    vid_lines = []
    for v in recent_vids:
        vid_id = v.get("video_id", "unknown")
        title = v.get("title", "Untitled")
        status = v.get("status", "pending_approval")
        vid_lines.append(f"- ID: {vid_id} | Title: '{title}' | Status: {status} | URL: https://youtu.be/{vid_id}")

    if not vid_lines:
        # P0 #23: no videos exist, so say so — never invent one.
        vid_lines.append("- No videos produced yet.")

    # Cloud runner info
    is_gh = os.getenv("GITHUB_ACTIONS") == "true"
    run_id = os.getenv("GITHUB_RUN_ID", "local")
    host_str = f"GitHub Actions Cloud (Runner #{run_id})" if is_gh else "Local Environment"

    return (
        f"--- REAL VERIFIED SYSTEM CONTEXT ---\n"
        f"Channel Name: {branding['channel_name']}\n"
        f"Like & Subscribe CTA: {branding['cta'].get('like_subscribe')}\n"
        f"Scheduler Paused: {paused}\n"
        f"Active Model Routing: Research={models.get('research')}, Script={models.get('script')}, Verify={models.get('verify')}, Image={models.get('image')}\n"
        f"AI Spend Cap: ${budget:.2f} USD/video\n"
        f"Pending Topics in Queue: {pending or 'None'}\n"
        f"Currently Running Topics: {running or 'None'}\n"
        f"Recent Production Videos:\n" + "\n".join(vid_lines) + "\n"
        f"Current Host: {host_str}\n"
        f"--- END VERIFIED SYSTEM CONTEXT ---"
    )


def chat_with_agent(user_id: int | str, user_prompt: str) -> str:
    """Process natural language input with deterministic intent resolution and grounded LLM fallback."""
    prompt = user_prompt.strip()
    if not prompt:
        branding = get_channel_branding()
        return (
            f"👋 *Hi! I'm your TubePilot AI Agent for {branding['channel_name']}.*\n\n"
            "I'm ready to help you brainstorm video ideas, critique scripts, inspect fact-checks, "
            "or dispatch production runs. Type `/menu` for the interactive dashboard or ask me anything!"
        )

    lower = prompt.lower()

    # =========================================================================
    # INTENT 1: Model Switching
    # =========================================================================
    model_switch_match = re.search(
        r"(?:switch|change|use|set)\s+(?:model\s+to\s+|to\s+model\s+|model\s+)?(gemini-2\.0|gemini-2\.5|gemini|deepseek-r1|deepseek|claude|imagen|flux)",
        lower,
    )
    if model_switch_match:
        from telegram_bot.bot import _handle_model_command
        target_model = model_switch_match.group(1)
        res = _handle_model_command([target_model])
        return f"🤖 *Model Configuration Updated!*\n\n{res['text']}"

    # =========================================================================
    # INTENT 2: Video Production Dispatch
    # =========================================================================
    run_match = re.search(
        r"^(?:make|create|generate|produce|run)\s+(?:a\s+)?(?:video\s+on|pipeline\s+for|breakdown\s+of|video\s+about)\s+(.+)$",
        lower,
    )
    if run_match:
        topic_target = run_match.group(1).strip(" .?!\"'")
        pillar = "tech_to_financial" if "tech" in topic_target.lower() else "finance_to_technical"
        dispatch_res = dispatch_pipeline_run(topic_target, pillar=pillar)
        return (
            f"🚀 *Production Run Dispatched!*\n\n"
            f"• *Topic:* `{topic_target}`\n"
            f"• *Pillar:* `{pillar}`\n"
            f"• *Queue ID:* `{dispatch_res['topic_id']}`\n"
            f"• *Dispatcher:* {dispatch_res['details']}\n\n"
            "The production engine is executing research extraction, script generation, and fact-check verification."
        )

    # =========================================================================
    # INTENT 3: Brainstorming Ideas
    # =========================================================================
    if any(k in lower for k in ("give me ideas", "topic ideas", "brainstorm", "suggest topics", "what topics", "video ideas")):
        lines = [
            "💡 *Trending Institutional Video Ideas:*",
            "",
        ]
        for idx, item in enumerate(TRENDING_INSTITUTIONAL_TOPICS[:4], 1):
            lines.append(f"*{idx}. {item['topic']}*")
            lines.append(f"• _Hook:_ \"{item['hook']}\"")
            lines.append(f"• _Pillar:_ `{item['pillar']}`")
            lines.append(f"• _Quick Queue:_ `/topic add \"{item['topic']}\"`\n")

        lines.append("Tap `/ideas` or `/menu` to add these topics to your queue with 1 tap!")
        return "\n".join(lines)

    # =========================================================================
    # INTENT 4: Script & Fact-Check Verification Review
    # =========================================================================
    if any(k in lower for k in ("show script", "view script", "fact check", "fact-check", "verification report", "audit report", "check script")):
        from telegram_bot.bot import _handle_script_command
        res = _handle_script_command([])
        return res["text"]

    # =========================================================================
    # INTENT 5: Topic Queue Status / Clear
    # =========================================================================
    if "clear queue" in lower or "empty queue" in lower:
        cleared = clear_topic_queue()
        return f"🗑 *Topic Queue Cleared!*\nRemoved `{cleared}` pending topics. Queue is now clean."

    if any(k in lower for k in ("queue", "upcoming topics", "pending topics", "what is in queue")):
        topics = list_topics_queue()
        if not topics:
            return "📋 *Topic Queue is currently empty.*\n\nYou can add topics using `/topic add <Title>` or tap `/ideas` for 1-tap suggestions."
        lines = ["📋 *Upcoming Topics in Queue:*"]
        for t in topics[:6]:
            lines.append(f"• `[{t.get('status', 'PENDING').upper()}]` {t.get('topic')} ({t.get('pillar', 'finance')})")
        lines.append("\nUse `/run <Topic>` to produce immediately, or `/menu` for interactive controls.")
        return "\n".join(lines)

    # =========================================================================
    # INTENT 6: System Status / Health Probe
    # =========================================================================
    if any(k in lower for k in ("system health", "status", "ping", "health report", "how is the system")):
        res = get_system_ping()
        return f"📊 *System Diagnostics:*\n\n{res['text']}"

    # =========================================================================
    # INTENT 7: Video Preview & Recent Uploads
    # =========================================================================
    if any(k in lower for k in ("preview", "watch video", "latest video", "recent videos", "show videos")):
        from telegram_bot.bot import _handle_preview_command
        res = _handle_preview_command([])
        return res["text"]

    # =========================================================================
    # CONVERSATIONAL LLM (Strictly Grounded, Anti-Hallucination)
    # =========================================================================
    grounding = _build_grounding_context()

    system_prompt = (
        "You are TubePilot Agent, an elite AI co-pilot and automated YouTube creator.\n"
        "You specialize in institutional financial technology and developer software business models "
        "(e.g., Stripe, Plaid, Visa, Nubank, Adyen, Cloudflare, interchange economics, P&L architecture).\n\n"
        f"{grounding}\n\n"
        "CRITICAL ANTI-HALLUCINATION INSTRUCTIONS:\n"
        "1. You are talking directly with your human YouTube channel owner via Telegram.\n"
        "2. Ground ALL factual claims strictly in the VERIFIED SYSTEM CONTEXT above.\n"
        "3. NEVER fabricate or make up fake YouTube video IDs or links (like youtube.com/watch?v=...).\n"
        "4. NEVER invent fake view counts, fake subscriber metrics, or fake pipeline run IDs.\n"
        "5. If the user asks about a video or topic that does NOT exist in the context, clearly inform them "
        "that it hasn't been produced yet, and offer to queue or run it with `/run <topic>`.\n"
        "6. Keep answers concise (under 200 words), sharp, institutional, direct, and formatted in clean Telegram Markdown.\n"
        "7. Suggest interactive bot commands when relevant (/menu, /run, /model, /preview, /script, /ideas)."
    )

    chain = [
        ["gemini", "gemini-2.0-flash"],
        ["gemini", "gemini-2.5-flash-lite"],
        ["openrouter", "deepseek/deepseek-chat"],
    ]

    try:
        response = call_with_fallback(
            chain=chain,
            system_prompt=system_prompt,
            user_prompt=prompt,
        )
        return response.content.strip()
    except Exception as e:
        logger.error(f"Agent chat LLM invocation failed: {e}")
        return (
            f"🤖 *TubePilot Agent Notice*\n\n"
            f"I encountered a temporary connection issue reaching the AI model pool: `{e}`\n\n"
            "You can test system health with `/ping` or view the interactive control deck with `/menu`."
        )
