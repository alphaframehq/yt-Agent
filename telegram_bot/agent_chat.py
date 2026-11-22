"""Agent Chat Assistant for TubePilot AI Telegram Control Plane.

Allows the YouTube channel owner to chat directly with the AI Agent in real-time.
Can answer domain questions, brainstorm video topics, critique scripts, review metrics,
and trigger or configure pipeline workflows.
"""

from __future__ import annotations

import os
import re
from typing import Optional
from loguru import logger

from config_manager import get_channel_branding, get_config, set_config
from db.dynamo import enqueue_topic, list_topics_queue
from telegram_bot.dispatch import dispatch_pipeline_run
from telegram_bot.health import get_system_ping
from tools.llm_provider import call_with_fallback
from tools.secrets import get_secret

__all__ = ["chat_with_agent"]


def chat_with_agent(user_id: int | str, user_prompt: str) -> str:
    """Process a natural language conversation from the user and respond as TubePilot Agent."""
    prompt = user_prompt.strip()
    if not prompt:
        branding = get_channel_branding()
        return (
            f"👋 *Hi! I'm your TubePilot AI Agent for {branding['channel_name']}.*\n\n"
            "I'm ready to help you brainstorm video ideas, critique scripts, monitor pipeline health, "
            "or dispatch production runs. What's on your mind?"
        )

    lower = prompt.lower()

    # 1. Intent: Model switching shortcut
    model_switch_match = re.search(
        r"(?:switch|change|use|set)\s+(?:model\s+to\s+|to\s+model\s+|model\s+)?(gemini-2\.0|gemini-2\.5|gemini|deepseek-r1|deepseek|claude|imagen|flux)",
        lower,
    )
    if model_switch_match:
        from telegram_bot.bot import _handle_model_command
        target_model = model_switch_match.group(1)
        res = _handle_model_command([target_model])
        return f"🤖 *Model Configuration Updated!*\n\n{res['text']}"

    # 2. Intent: Run or generate video
    run_match = re.search(
        r"^(?:make|create|generate|produce|run)\s+(?:a\s+)?(?:video\s+on|pipeline\s+for|breakdown\s+of|video\s+about)\s+(.+)$",
        lower,
    )
    if run_match:
        topic_target = run_match.group(1).strip(" .?!\"'")
        dispatch_res = dispatch_pipeline_run(topic_target)
        return (
            f"🚀 *Production Run Dispatched!*\n\n"
            f"• *Topic:* `{topic_target}`\n"
            f"• *Queue ID:* `{dispatch_res['topic_id']}`\n"
            f"• *Status:* {dispatch_res['details']}\n\n"
            "I'm executing research claims extraction, script drafting, fact-check verification, and video rendering now."
        )

    # 3. Intent: System status / ping inquiry
    if any(k in lower for k in ("system health", "status", "ping", "health report", "how is the system")):
        res = get_system_ping()
        return f"📊 *System Diagnostics:*\n\n{res['text']}"

    # 4. Intent: Topic queue inquiry
    if "topic queue" in lower or "upcoming topics" in lower or "what topics" in lower:
        topics = list_topics_queue()
        if not topics:
            return "📋 *Topic Queue is currently empty.*\n\nYou can add topics using `/topic add <Title>` or ask me: *'make a video on Visa's payment rails'*."
        lines = ["📋 *Upcoming Topics in Queue:*"]
        for t in topics[:6]:
            lines.append(f"• `[{t.get('status', 'PENDING').upper()}]` {t.get('topic')}")
        return "\n".join(lines)

    # 5. Conversational LLM call
    branding = get_channel_branding()
    channel_name = branding.get("channel_name", "TubePilot AI")
    cta = branding.get("cta", {}).get("like_subscribe", "")
    active_models = get_config("active_models") or {}
    budget = get_config("budget_ai_per_video", 0.45)

    topics = list_topics_queue()
    recent_topics = [f"- {t.get('topic')}" for t in topics[:4]] if topics else ["- No pending topics in queue"]
    topics_summary = "\n".join(recent_topics)

    system_prompt = (
        "You are TubePilot Agent, an elite AI co-pilot and automated YouTube creator.\n"
        f"Channel: '{channel_name}'\n"
        f"Channel CTA: '{cta}'\n"
        f"Active Model Routing: {active_models}\n"
        f"AI Spend Budget: ${budget} USD per video\n"
        f"Current Topic Queue:\n{topics_summary}\n\n"
        "Your Persona & Directives:\n"
        "1. You specialize in deep institutional fintech, financial engineering, and developer tech business models "
        "(e.g., Stripe, Plaid, Visa, Nubank, Adyen, Cloudflare, payment gateways, interchange economics, P&L architecture).\n"
        "2. You are chatting directly with your YouTube channel owner via Telegram.\n"
        "3. You can answer technical/financial questions, suggest 10/10 hook ideas, review video scripts, discuss audience retention, "
        "and suggest high-performing video topics.\n"
        "4. Tone: Concise, insightful, confident, institutional yet engaging for curious software engineers and investors.\n"
        "5. Keep responses concise (under 250 words) and formatted in clean Telegram Markdown (bold, bullet points, backticks).\n"
        "6. If user wants to produce a video, remind them they can type `/run <topic>` or simply say 'make a video on <topic>'."
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
            "You can test system health with `/ping` or verify active models with `/model`."
        )
