"""Topic safety / niche filter for TubePilot AI.

The niche is AI — anything new in AI (26 Nov 2026 user correction):
new tools, projects, discoveries, inventions, AI scams/frauds,
predictions, strategy, formulas, ML, LLM training, pros/cons, leaks
and clues, AI conspiracy theories. A bad topic must never reach an
LLM call, a research tool, or the YouTube API. Two gates, applied at
dequeue time (``scripts/dev_run.py``) and again at pipeline entry
(``orchestration.graph.TubePilotGraph.run``):

1. **Blocklist** — explicit / sexual / adult terms. A topic matching
   one is rejected outright, whatever else it contains. Matching is
   on whole words / phrases (``\\b`` boundaries) so "Sussex" does not
   trip on "sex".
2. **AI-niche allowlist** — the topic must carry an AI signal (AI
   keywords, an AI company/tool name, or an AI angle such as
   training, leaks, scams, predictions). Pure finance/investing with
   no AI angle, pure celebrity gossip, and unrelated viral stunts
   have no AI signal and are rejected as off-niche.

Rejected topics are marked ``failed`` in the Sheets queue by the
caller (``dev_run.py``) so they are never dequeued again; this module
itself is pure (no I/O) so it is trivially unit-testable.
"""

from __future__ import annotations

import re
from typing import Tuple

__all__ = [
    "check_topic",
    "is_topic_allowed",
    "BLOCKED_PATTERNS",
    "AI_KEYWORDS",
    "KNOWN_AI_NAMES",
]

# Explicit / sexual / adult terms. Whole-word / whole-phrase match.
# (Terms that would false-positive on ordinary words — e.g. "ass" on
# "asset", "cock" on "cocktail" — are deliberately NOT in this list.)
_BLOCKED_TERMS: tuple[str, ...] = (
    "sex",
    "sexual",
    "porn",
    "pornography",
    "pornographic",
    "nude",
    "naked",
    "erotic",
    "erotica",
    "xxx",
    "escort",
    "escorts",
    "onlyfans",
    "masturbat",
    "orgasm",
    "fetish",
    "hentai",
    "camgirl",
    "cam girl",
    "stripper",
    "strip club",
    "brothel",
    "prostitut",
    "hookup",
    "hook up",
    "one night stand",
    "kamasutra",
    "kama sutra",
    "blowjob",
    "handjob",
    "penis",
    "vagina",
    "breast",
    "boobs",
    "pussy",
    "dick",
    "horny",
    "sexy",
    "seduce",
    "seduction",
    "aphrodisiac",
    "playboy",
    "adult video",
    "adult film",
)

BLOCKED_PATTERNS: tuple["re.Pattern[str]", ...] = tuple(
    re.compile(r"\b" + re.escape(term.strip()) + r"\w*\b", re.IGNORECASE)
    if term.strip() and " " not in term.strip()
    else re.compile(re.escape(term.strip()), re.IGNORECASE)
    for term in _BLOCKED_TERMS
)

# AI signal words/phrases. Short tokens (ai, ml, gpt, rag, agi, gpu)
# match on word boundaries only; longer terms match as substrings so
# "training" also matches "trained", "robot" also matches "robotics".
AI_KEYWORDS: tuple[str, ...] = (
    # Core AI
    "ai", "artificial intelligence", "machine learning", "deep learning",
    "neural network", "neural", "llm", "large language model",
    "generative ai", "genai", "gen ai", "chatbot", "chatgpt",
    "transformer", "diffusion model", "multimodal",
    # How AI is built
    "training", "fine-tun", "prompt", "prompt engineering",
    "inference", "embedding", "rag", "retrieval-augmented",
    "reinforcement learning", "supervised learning",
    "unsupervised learning", "foundation model",
    "open weights", "open source model", "quantiz", "distill",
    "dataset", "context window",
    # What AI does / where it shows up
    "ai agent", "agentic", "automation", "automated", "robot",
    "computer vision", "nlp", "natural language", "speech",
    "voice ai", "image generation", "video generation",
    "text-to-image", "text-to-video", "text-to-speech",
    "self-driving", "autonomous", "humanoid", "drone",
    "algorithm", "predictive", "recommendation",
    # AI in the world: news angles the channel covers
    "ai tool", "ai app", "ai startup", "ai model", "ai news",
    "ai prediction", "ai strategy", "ai research", "ai paper",
    "ai breakthrough", "ai discovery", "ai invention",
    "ai leak", "ai conspiracy", "ai scam", "ai fraud",
    "deepfake", "synthetic media", "ai-generated",
    "agi", "superintelligence", "alignment", "hallucination",
    "jailbreak", "prompt injection", "bias", "censorship",
    "gpu", "tpu", "nvidia", "data center",
    # Outcomes / analysis angles
    "pros and cons", "advantages", "disadvantages",
    "future of ai", "ai future", "ai revolution",
    "will ai replace", "ai vs human", "ai job",
)

# AI companies, labs, products and tools. A topic naming one of
# these is on-niche even without a generic AI keyword. Pure-finance
# companies (Stripe, Revolut, PayPal, Visa, ...) are deliberately
# NOT here: a finance topic with no AI angle is off-niche.
KNOWN_AI_NAMES: tuple[str, ...] = (
    "openai", "anthropic", "deepmind", "google deepmind", "xai",
    "mistral", "meta ai", "hugging face", "huggingface",
    "chatgpt", "gpt-", "gpt ", "gemini", "claude", "copilot",
    "grok", "llama", "midjourney", "dall-e", "dalle", "sora",
    "runway", "perplexity", "cursor", "windsurf", "elevenlabs",
    "suno", "udio", "pika", "kling", "veo", "imagen",
    "stable diffusion", "cohere", "character ai", "replika",
    "nvidia", "pytorch", "tensorflow", "kaggle", "langchain",
    "samsung ai", "apple intelligence",
)

# Short tokens that must match as whole words (not substrings, so
# "ai" does not match "said" and "ml" does not match "html").
_SHORT_TOKEN_RE: dict[str, "re.Pattern[str]"] = {
    token: re.compile(r"\b" + re.escape(token) + r"\b", re.IGNORECASE)
    for token in ("ai", "ml", "gpt", "rag", "agi", "gpu", "tpu", "nlp")
}


def _contains_blocked(text: str) -> str | None:
    """Return the blocked term matched in *text*, or ``None``."""
    lowered = text.lower()
    for term, pattern in zip(_BLOCKED_TERMS, BLOCKED_PATTERNS):
        if pattern.search(lowered):
            return term.strip()
    return None


def _has_ai_signal(text: str) -> bool:
    """Return True if *text* carries an AI-niche signal."""
    lowered = " " + text.lower() + " "
    if any(name in lowered for name in KNOWN_AI_NAMES):
        return True
    for token, pattern in _SHORT_TOKEN_RE.items():
        if pattern.search(text):
            return True
    return any(keyword.strip() in lowered for keyword in AI_KEYWORDS)


def check_topic(topic: str | None, pillar: str | None = None) -> Tuple[bool, str]:
    """Check *topic* against the safety blocklist and the AI-niche allowlist.

    Returns ``(allowed, reason)``. ``reason`` is ``"ok"`` when allowed,
    otherwise a short machine-readable reason (``blocked-term:<term>``
    or ``off-niche: no AI signal``) for logging and for marking the
    queue row ``failed``.
    """
    if not topic or not str(topic).strip():
        return False, "empty-topic"
    text = str(topic).strip()
    blocked = _contains_blocked(text)
    if blocked:
        return False, f"blocked-term:{blocked}"
    if not _has_ai_signal(text):
        return False, "off-niche: no AI signal"
    return True, "ok"


def is_topic_allowed(topic: str | None, pillar: str | None = None) -> bool:
    """Convenience wrapper: True when :func:`check_topic` allows *topic*."""
    allowed, _ = check_topic(topic, pillar)
    return allowed
