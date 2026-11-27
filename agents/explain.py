"""Explanatory visuals (28 Nov 2026, Dev direction).

"Jo bol raha hai, usko screen par visuals me samjhaye" — the visual
must EXPLAIN the narration, not just move decoratively. A viewer with
the sound OFF should still roughly follow what each scene is about.

How it works
------------
:func:`derive_visual_beat` reads one scene's narration and derives a
per-scene "visual beat": a kind (``chat`` / ``wallet`` / ``stat`` /
``comparison`` / ``diagram`` / ``generic``) plus the one entity, the
one number and a short (<= 4 word) label that carry the meaning.
:func:`draw_explainer_frame` renders that beat as one Pillow frame at
time ``t`` — chat bubbles sliding in, coins flying into a wallet, a
number counting up, bars growing, diagram boxes connecting — so the
animation itself is the explanation. ``agents.assemble`` turns the
frames into a MoviePy ``VideoClip`` (``make_frame``) and uses it as
the scene visual whenever the scene has no real stock *video* and no
chart; stock stills stay as the fallback for ``generic`` beats.

Rules (same as the Shorts no-narration-text rule, 27 Nov 2026):
- The screen NEVER shows the narration sentence. On-screen text is
  only short labels (entity names, bubble shorthand, a figure) of at
  most 4 words — the kind of words a diagram would carry.
- Everything is Pillow + MoviePy only: no TextClip (no ImageMagick
  on the runners), no new paid service, no network.
- Deterministic: the same narration always derives the same beat,
  so renders are reproducible and unit-testable without MoviePy.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "derive_visual_beat",
    "draw_explainer_frame",
    "draw_stat_card_frame",
    "parse_stat_number",
    "EXPLAINER_KINDS",
]

EXPLAINER_KINDS = ("chat", "wallet", "stat", "comparison", "diagram", "generic")

BG = (14, 17, 23)          # #0E1117 — channel dark background
PANEL = (24, 30, 44)
TEAL = (0, 212, 170)       # #00D4AA — primary accent
CORAL = (255, 107, 107)    # #FF6B6B — secondary accent
PURPLE = (121, 40, 202)
BLUE = (0, 112, 243)
FG = (230, 237, 243)
MUTED = (160, 175, 195)
CARD_BG = (10, 14, 20, 165)

_NUMBER_RE = re.compile(
    r"\$?\s?(\d+(?:\.\d+)?)\s*(billion|million|trillion)\b", re.IGNORECASE
)
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_PLAIN_MONEY_RE = re.compile(r"\$(\d+(?:\.\d+)?)\b")
_CHAT_RE = re.compile(
    r"\b(chatting|group chat|chat in a group|conversation|discuss|"
    r"message|decide together|talk to each other|"
    r"agents? (?:chat|talk|discuss|decide))\b",
    re.IGNORECASE,
)
_WALLET_RE = re.compile(
    r"\b(wallet|money|spend|spending|pay|pays|paying|payment|buy|buys|"
    r"purchase|balance|dollar|cost|price|checkout)\b",
    re.IGNORECASE,
)
_COMPARE_RE = re.compile(
    r"\b(versus|\bvs\b|compared?|more than|less than|before|after|"
    r"instead|used to take|faster than|slower than)\b",
    re.IGNORECASE,
)
_TIME_WORDS = ("seconds", "minutes", "hours", "days", "weeks", "months")
_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "with",
    "and", "or", "but", "is", "are", "was", "were", "be", "been",
    "by", "from", "as", "it", "its", "this", "that", "these", "those",
    "can", "could", "will", "would", "just", "now", "over", "into",
    "before", "after", "remains", "remain", "officially", "making",
    "one", "what", "when", "how", "why", "there", "here", "then",
}
_ACRONYMS = {"ai": "AI", "ipo": "IPO", "usd": "USD", "ceo": "CEO", "chatgpt": "ChatGPT"}


def _titlecase(text: str) -> str:
    """Title-case, keeping acronyms and brand names with internal
    capitals (ChatGPT, AI) exactly as written."""
    out = []
    for w in text.split():
        if any(c.isupper() for c in w[1:]):
            out.append(w)
        else:
            out.append(_ACRONYMS.get(w.lower(), w.capitalize()))
    return " ".join(out)


def _content_words(text: str) -> list:
    return [
        w for w in re.findall(r"[A-Za-z][A-Za-z'\-]*", str(text or ""))
        if w.lower() not in _STOPWORDS
    ]


def _find_number(text: str) -> Optional[Dict[str, Any]]:
    """One number from the narration, for count-up / balance display.

    Returns ``{"value", "prefix", "suffix", "text"}`` or None.
    ``$81 billion`` -> value 81, prefix ``$``, suffix ``B``;
    ``42%`` -> value 42, suffix ``%``; ``$100`` -> value 100.
    Word figures ("millions") have no countable value -> None.
    """
    m = _NUMBER_RE.search(text)
    if m:
        suffix = {"billion": "B", "million": "M", "trillion": "T"}.get(
            (m.group(2) or "").lower(), ""
        )
        value = float(m.group(1))
        # "81 billion dollars" is dollars even without a $ sign.
        prefix = "$" if ("$" in m.group(0) or "dollar" in text.lower()) else ""
        return {
            "value": value,
            "prefix": prefix,
            "suffix": suffix,
            "text": f"{prefix}{m.group(1)}{suffix}",
        }
    m = _PCT_RE.search(text)
    if m:
        return {
            "value": float(m.group(1)),
            "prefix": "",
            "suffix": "%",
            "text": f"{m.group(1)}%",
        }
    m = _PLAIN_MONEY_RE.search(text)
    if m:
        return {
            "value": float(m.group(1)),
            "prefix": "$",
            "suffix": "",
            "text": f"${m.group(1)}",
        }
    return None


# Words that are only capitalised because they start a sentence —
# never the scene's entity ("Inside", "When", "One", ...).
_ENTITY_SKIP = {
    "inside", "when", "one", "this", "that", "the", "but", "and",
    "with", "from", "while", "after", "before", "every", "each",
    "what", "where", "why", "how", "whole", "there", "here",
}


def _find_entity(text: str) -> str:
    """Short entity for the scene: the best capitalised phrase —
    a brand name with internal capitals (ChatGPT) or a multi-word
    phrase beats a sentence-starter ("Inside", "One"). Falls back
    to the first content words. Never more than 3 words."""
    best = None
    best_score = -1
    for m in re.finditer(r"\b([A-Z][A-Za-z]*(?:\s+[A-Z][a-z]+){0,2})\b", text or ""):
        cand = m.group(1)
        words = [w for w in cand.split()
                 if w.lower() not in _STOPWORDS and len(w) > 1]
        if not words or all(w.lower() in _ENTITY_SKIP for w in words):
            continue
        score = 0
        # Brand name: mixed case with an internal capital (ChatGPT).
        # All-caps words (AI) are acronyms, not brands — they score
        # lower so a brand earlier in the sentence still wins.
        if any(any(c.islower() for c in w) and any(c.isupper() for c in w[1:])
               for w in words):
            score += 4
        elif any(w.isupper() and len(w) >= 2 for w in words):
            score += 3  # acronym: AI, IPO
        if len(words) > 1:
            score += 1
        if m.start() > 0:
            score += 1
        if score > best_score:
            best_score, best = score, " ".join(words[:3])
    if best:
        return _titlecase(best)
    words = _content_words(text)
    return _titlecase(" ".join(words[:3])) or "Key Idea"


def _comparison_labels(text: str) -> Tuple[str, str]:
    """Two short labels for a comparison beat: time words when the
    narration compares durations ("seconds" vs "hours"), else
    Before / After."""
    low = (text or "").lower()
    found = [w for w in _TIME_WORDS if re.search(rf"\b{w}\b", low)]
    if len(found) >= 2:
        return (_titlecase(found[0]), _titlecase(found[1]))
    if "before" in low or "used to" in low:
        return ("Before", "Now")
    return ("Option A", "Option B")


def derive_visual_beat(narration: str) -> Dict[str, Any]:
    """Derive the explanatory visual beat for one narration.

    Priority is meaning, not keywords alone: a group of agents
    deciding is a ``chat`` even when money is mentioned; a wallet
    scene with a figure carries the figure as a balance count-up;
    a bare figure is a ``stat``; a named thing with no action is a
    ``diagram``. ``generic`` (keyword label + pulse) is the last
    resort and means "no explanatory visual could be derived".
    """
    text = str(narration or "").strip()
    number = _find_number(text)
    entity = _find_entity(text)
    words = _content_words(text)
    label = _titlecase(" ".join(words[:4])) or entity

    if _CHAT_RE.search(text):
        kind = "chat"
    elif _COMPARE_RE.search(text):
        # A comparison outranks a wallet/stat mention: "finishes in
        # seconds what used to take hours — and every payment is
        # logged" is ABOUT the comparison, the money is secondary.
        kind = "comparison"
    elif _WALLET_RE.search(text):
        kind = "wallet"
    elif number is not None:
        kind = "stat"
    elif len(re.findall(r"\b[A-Z][a-z]+\b", text)) >= 2:
        kind = "diagram"
    else:
        kind = "generic"

    beat: Dict[str, Any] = {
        "kind": kind,
        "entity": entity,
        "label": label,
        "number": number,
        "sublabel": "",
    }
    if kind == "comparison":
        a, b = _comparison_labels(text)
        beat["compare_a"] = a
        beat["compare_b"] = b
        beat["label"] = f"{a} vs {b}"
        beat["entity"] = f"{a} vs {b}"
    if kind == "diagram":
        beat["label"] = entity
    if kind == "generic":
        beat["label"] = entity
    return beat


# ---------------------------------------------------------------------------
# Frame drawing (Pillow; one frame per t, called by MoviePy make_frame)
# ---------------------------------------------------------------------------

def _load_font(size: int, bold: bool = True):
    """Cached font load: ``tools.image_gen._load_font`` re-opens the
    TTF file on every call, and an explainer frame loads several
    fonts — at 30 fps that is thousands of file opens per render.
    Cache by (size, bold) for the life of the process."""
    key = (size, bold)
    font = _FONT_CACHE.get(key)
    if font is None:
        from tools.image_gen import _load_font as _lf

        font = _lf(size, bold=bold)
        _FONT_CACHE[key] = font
    return font


_FONT_CACHE: dict = {}


def _ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def _ease_in_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _rounded(draw, box, radius, **kw):
    draw.rounded_rectangle(box, radius=radius, **kw)


def _text_centred(draw, cx, y, text, font, fill):
    w = draw.textlength(text, font=font)
    draw.text((cx - w / 2, y), text, font=font, fill=fill)
    return w


def _label_pill(draw, width, text, font_size=44):
    """Top label pill: the scene entity, small, never a sentence."""
    from PIL import ImageDraw  # noqa: F401  (kept local; draw is passed in)

    font = _load_font(font_size, bold=True)
    tw = draw.textlength(text, font=font)
    pad_x, pad_y = 36, 14
    bw, bh = tw + pad_x * 2, font_size + pad_y * 2
    x0 = (width - bw) / 2
    y0 = 36
    _rounded(draw, [x0, y0, x0 + bw, y0 + bh], radius=bh / 2,
             fill=(24, 30, 44, 255), outline=TEAL, width=3)
    _text_centred(draw, width / 2, y0 + pad_y - 6, text, font, FG)


def _draw_chat(img_draw, beat, t, duration, w, h):
    """Agents chatting: 3 agent dots + bubbles sliding in, one per
    beat, with a typing indicator on the next bubble."""
    draw = img_draw
    portrait = h > w
    number = beat.get("number") or {}
    ntext = number.get("text", "")
    bubbles = [
        ("Here's the plan", TEAL),
        (f"Pay {ntext}" if ntext else "What does it cost?", CORAL),
        ("Agreed — go", PURPLE),
    ]
    agents = [("A1", TEAL), ("A2", CORAL), ("A3", PURPLE)]
    n = len(bubbles)
    # Bubble i appears at (i+1)/(n+1) of the scene; slide-in 0.45s.
    margin = w * 0.08
    if portrait:
        ax, ay0, gap = w * 0.5, h * 0.20, h * 0.055
        av_r = 54
        for j, (ini, col) in enumerate(agents):
            cx = w * (0.22 + 0.28 * j)
            # Gentle bob so the scene never sits still even after all
            # bubbles are in (the old tail was static except the dots).
            cy = ay0 + 5 * math.sin(t * 2.0 + j * 1.5)
            draw.ellipse([cx - av_r, cy - av_r, cx + av_r, cy + av_r],
                         fill=col, outline=(255, 255, 255), width=4)
            _text_centred(draw, cx, cy - 26, ini, _load_font(44), (255, 255, 255))
        top = ay0 + 110
        bh_each = (h * 0.52) / n
        for i, (line, col) in enumerate(bubbles):
            appear = (i + 0.5) / (n + 1)
            p = _ease_out((t / duration - appear) / 0.12) if duration else 1.0
            if p <= 0:
                # Typing dots for the next bubble.
                if i == 0 or _ease_out((t / duration - i / (n + 1)) / 0.12) > 0:
                    pass
                continue
            slide = (1 - p) * 90
            y = top + i * bh_each
            font = _load_font(52)
            tw = draw.textlength(line, font=font)
            bw = min(tw + 70, w - margin * 2)
            bx = margin + slide if i % 2 == 0 else w - margin - bw - slide
            _rounded(draw, [bx, y, bx + bw, y + 110], radius=34,
                     fill=(24, 30, 44, 255), outline=col, width=4)
            draw.text((bx + 34, y + 28), line, font=font, fill=FG)
    else:
        av_r = 62
        ay = h * 0.30
        for j, (ini, col) in enumerate(agents):
            cx = w * (0.20 + 0.30 * j)
            cy = ay + 5 * math.sin(t * 2.0 + j * 1.5)
            draw.ellipse([cx - av_r, cy - av_r, cx + av_r, cy + av_r],
                         fill=col, outline=(255, 255, 255), width=4)
            _text_centred(draw, cx, cy - 30, ini, _load_font(48), (255, 255, 255))
        top = h * 0.44
        bh_each = (h * 0.42) / n
        for i, (line, col) in enumerate(bubbles):
            # First bubble appears early (~1/8 in), then one per beat.
            appear = (i + 0.5) / (n + 1)
            p = _ease_out((t / duration - appear) / 0.12) if duration else 1.0
            if p <= 0:
                continue
            slide = (1 - p) * 110
            y = top + i * bh_each
            font = _load_font(56)
            tw = draw.textlength(line, font=font)
            bw = tw + 80
            bx = margin + slide if i % 2 == 0 else w - margin - bw - slide
            _rounded(draw, [bx, y, bx + bw, y + 120], radius=36,
                     fill=(24, 30, 44, 255), outline=col, width=4)
            draw.text((bx + 40, y + 30), line, font=font, fill=FG)
    # Typing dots: three pulsing dots under the last visible bubble.
    visible = sum(
        1 for i in range(n)
        if _ease_out((t / duration - (i + 0.5) / (n + 1)) / 0.12) > 0
    ) if duration else n
    if 0 < visible < n:
        cy = (h * 0.88 if portrait else h * 0.90)
        for d in range(3):
            r = 10 + 5 * math.sin(t * 6 + d * 1.2)
            cx = w / 2 + (d - 1) * 36
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=MUTED)


def _draw_wallet(img_draw, beat, t, duration, w, h):
    """A wallet with coins flying into it + a balance count-up."""
    draw = img_draw
    portrait = h > w
    number = beat.get("number") or {}
    cx, cy = w / 2, h * (0.42 if portrait else 0.40)
    ww, wh = (w * 0.62, h * 0.20) if portrait else (w * 0.42, h * 0.30)
    # Wallet body.
    _rounded(draw, [cx - ww / 2, cy - wh / 2, cx + ww / 2, cy + wh / 2],
             radius=36, fill=(64, 48, 28, 255), outline=TEAL, width=5)
    _rounded(draw, [cx + ww * 0.18, cy - wh * 0.22, cx + ww / 2, cy + wh * 0.22],
             radius=24, fill=(24, 30, 44, 255), outline=(255, 255, 255), width=3)
    # Pulsing clasp dot: the wallet keeps breathing after the last
    # coin lands, so the tail of the scene is never frozen.
    pr = 12 + 4 * math.sin(t * 3.0)
    draw.ellipse([cx + ww * 0.30 + 16 - pr, cy - pr,
                  cx + ww * 0.30 + 16 + pr, cy + pr], fill=TEAL)
    # Card peeking out of the wallet, sliding up with t.
    card_slide = _ease_out(t / max(duration, 0.01)) * wh * 0.35
    _rounded(draw, [cx - ww * 0.38, cy - wh / 2 - 70 + card_slide,
                    cx + ww * 0.05, cy - wh / 2 + 30 + card_slide],
             radius=18, fill=BLUE, outline=(255, 255, 255), width=3)
    draw.text((cx - ww * 0.38 + 22, cy - wh / 2 - 48 + card_slide),
              "AI PAY", font=_load_font(40), fill=(255, 255, 255))
    # Coins fly in from the right, staggered, arcing into the slot.
    n_coins = 5
    for i in range(n_coins):
        start = i * (duration / (n_coins + 1))
        p = _clamp01((t - start) / (duration * 0.22)) if duration else 1.0
        if p <= 0:
            continue
        if p >= 1:
            continue  # coin is inside the wallet now
        x = w * 0.95 - p * (w * 0.95 - (cx + ww * 0.30))
        y = cy - wh * 0.55 + math.sin(p * math.pi) * -70
        r = 34 - p * 10
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(255, 200, 60),
                     outline=(255, 255, 255), width=3)
        _text_centred(draw, x, y - 22, "$", _load_font(40), (60, 40, 0))
    # Balance count-up (when the narration names a figure).
    by = h * (0.74 if portrait else 0.76)
    if number:
        p = _ease_out(t / max(duration, 0.01))
        shown = number["value"] * p
        val = f"{shown:.0f}" if number["value"] >= 10 or float(number["value"]).is_integer() else f"{shown:.1f}"
        big_text = f"{number.get('prefix', '')}{val}{number.get('suffix', '')}"
        _text_centred(draw, w / 2, by, big_text, _load_font(150 if portrait else 130), FG)
        _text_centred(draw, w / 2, by + (170 if portrait else 150),
                      "BALANCE", _load_font(44), MUTED)
    else:
        _text_centred(draw, w / 2, by, "MONEY IN  •  MONEY OUT",
                      _load_font(56), FG)


def _draw_stat(img_draw, beat, t, duration, w, h):
    """Count-up number + growing bar + short label."""
    draw = img_draw
    portrait = h > w
    number = beat.get("number") or {}
    p = _ease_out(t / max(duration, 0.01))
    cy = h * (0.42 if portrait else 0.40)
    if number:
        shown = number["value"] * p
        val = f"{shown:.0f}" if float(number["value"]).is_integer() else f"{shown:.1f}"
        big_text = f"{number.get('prefix', '')}{val}{number.get('suffix', '')}"
    else:
        big_text = beat.get("entity", "KEY FACT").upper()
    _text_centred(draw, w / 2, cy, big_text,
                  _load_font(170 if portrait else 150), FG)
    # Growing bar.
    bw = w * 0.72
    bx0 = (w - bw) / 2
    by = cy + (200 if portrait else 180)
    draw.rounded_rectangle([bx0, by, bx0 + bw, by + 34], radius=17, fill=PANEL)
    draw.rounded_rectangle([bx0, by, bx0 + bw * p, by + 34], radius=17, fill=TEAL)
    _text_centred(draw, w / 2, by + 60, beat.get("label", "")[:40],
                  _load_font(52), MUTED)


def _draw_comparison(img_draw, beat, t, duration, w, h):
    """Two bars growing to different lengths — Before vs Now."""
    draw = img_draw
    portrait = h > w
    a = beat.get("compare_a", "Before")
    b = beat.get("compare_b", "Now")
    # Bars show TIME TAKEN: the slow side ("Hours") is honestly
    # longer, but the FAST side ("Seconds") is the winner — it gets
    # the teal highlight even though its bar is shorter. (The old
    # version highlighted the long bar, which read as "Hours wins".)
    rows = [(a, 0.55, TEAL), (b, 0.95, MUTED)]
    top = h * (0.34 if portrait else 0.32)
    row_h = h * (0.16 if portrait else 0.18)
    bw = w * 0.70
    bx0 = (w - bw) / 2
    for i, (label, frac, col) in enumerate(rows):
        y = top + i * row_h
        appear = 0.15 + i * 0.25
        p = _ease_out((t / max(duration, 0.01) - appear) / 0.45)
        draw.text((bx0, y - 58), label, font=_load_font(52), fill=FG)
        draw.rounded_rectangle([bx0, y, bx0 + bw, y + 44], radius=22, fill=PANEL)
        if p > 0:
            draw.rounded_rectangle([bx0, y, bx0 + bw * frac * p, y + 44],
                                   radius=22, fill=col)


def _draw_diagram(img_draw, beat, t, duration, w, h):
    """Three labelled boxes connecting in sequence with arrows."""
    draw = img_draw
    portrait = h > w
    entity = beat.get("entity", "Key Idea")
    words = _content_words(entity) or ["Key", "Idea"]
    labels = words[:3]
    while len(labels) < 3:
        labels.append(f"Step {len(labels) + 1}")
    n = 3
    if portrait:
        bw, bh = w * 0.72, h * 0.13
        bx0 = (w - bw) / 2
        top = h * 0.28
        gap = h * 0.20
        for i, lab in enumerate(labels):
            appear = (i + 1) / (n + 1)
            p = _ease_out((t / max(duration, 0.01) - appear) / 0.15)
            if p <= 0:
                continue
            y = top + i * gap
            _rounded(draw, [bx0, y, bx0 + bw, y + bh], radius=28,
                     fill=PANEL, outline=TEAL if i == n - 1 else FG, width=4)
            _text_centred(draw, w / 2, y + bh / 2 - 28, _titlecase(lab),
                          _load_font(54), FG)
            if i < n - 1:
                ay0, ay1 = y + bh + 8, y + gap - 8
                prog = _clamp01((t / max(duration, 0.01) - appear - 0.12) / 0.2)
                if prog > 0:
                    ymid = ay0 + (ay1 - ay0) * prog
                    draw.line([w / 2, ay0, w / 2, ymid], fill=TEAL, width=6)
                    draw.polygon([(w / 2 - 14, ymid - 18), (w / 2 + 14, ymid - 18),
                                  (w / 2, ymid + 6)], fill=TEAL)
    else:
        bw, bh = w * 0.26, h * 0.30
        top = h * 0.38
        gap = w * 0.34
        bx0 = (w - gap * (n - 1) - bw) / 2
        for i, lab in enumerate(labels):
            appear = (i + 1) / (n + 1)
            p = _ease_out((t / max(duration, 0.01) - appear) / 0.15)
            if p <= 0:
                continue
            x = bx0 + i * gap
            _rounded(draw, [x, top, x + bw, top + bh], radius=28,
                     fill=PANEL, outline=TEAL if i == n - 1 else FG, width=4)
            _text_centred(draw, x + bw / 2, top + bh / 2 - 28, _titlecase(lab),
                          _load_font(54), FG)
            if i < n - 1:
                ax0, ax1 = x + bw + 10, x + gap - 10
                prog = _clamp01((t / max(duration, 0.01) - appear - 0.12) / 0.2)
                if prog > 0:
                    xmid = ax0 + (ax1 - ax0) * prog
                    draw.line([ax0, top + bh / 2, xmid, top + bh / 2],
                              fill=TEAL, width=6)
                    draw.polygon([(xmid - 18, top + bh / 2 - 14),
                                  (xmid - 18, top + bh / 2 + 14),
                                  (xmid + 6, top + bh / 2)], fill=TEAL)


def _draw_generic(img_draw, beat, t, duration, w, h):
    """Last resort: entity label with a pulsing ring."""
    draw = img_draw
    cx, cy = w / 2, h * 0.44
    pulse = 1.0 + 0.06 * math.sin(t * 3.0)
    r = int(min(w, h) * 0.16 * pulse)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=TEAL, width=6)
    draw.ellipse([cx - r * 0.7, cy - r * 0.7, cx + r * 0.7, cy + r * 0.7],
                 outline=MUTED, width=3)
    _text_centred(draw, cx, cy - 30, beat.get("entity", "KEY IDEA").upper()[:24],
                  _load_font(64), FG)


_DRAWERS = {
    "chat": _draw_chat,
    "wallet": _draw_wallet,
    "stat": _draw_stat,
    "comparison": _draw_comparison,
    "diagram": _draw_diagram,
    "generic": _draw_generic,
}


def draw_explainer_frame(t: float, beat: Dict[str, Any], duration: float,
                         width: int, height: int):
    """One RGB frame (numpy uint8 array) of the explanatory visual.

    Pure Pillow + numpy — no MoviePy needed, so unit tests can call
    it directly and assert frames change over time (real motion) and
    contain no narration text (only short labels).
    """
    import numpy as np
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)
    # Faint dot grid so the background is never flat black.
    step = 90
    for gx in range(step // 2, width, step):
        for gy in range(step // 2, height, step):
            draw.ellipse([gx - 2, gy - 2, gx + 2, gy + 2], fill=(30, 38, 56))
    _label_pill(draw, width, (beat.get("entity") or "Key Idea")[:28])
    drawer = _DRAWERS.get(beat.get("kind"), _draw_generic)
    drawer(draw, beat, float(t), float(duration), width, height)
    return np.asarray(img)


def parse_stat_number(big: str) -> Optional[Dict[str, Any]]:
    """Parse a stat-card big figure for count-up: ``$81B`` -> 81,
    ``42%`` -> 42. Word figures (``Millions``) return None — they
    cannot count up and the card stays static."""
    text = str(big or "").strip()
    m = re.match(r"^\$?(\d+(?:\.\d+)?)\s*([BMKT%]?)$", text)
    if not m:
        return None
    return {
        "value": float(m.group(1)),
        "prefix": "$" if text.startswith("$") else "",
        "suffix": m.group(2) or "",
        "text": text,
    }


def draw_stat_card_frame(t: float, big: str, label: str, duration: float,
                         width: int = 920, height: int = 480):
    """One RGBA frame of an animated stat card (count-up number).

    Same card art as :func:`agents.assemble._render_stat_card` (dark
    rounded card, teal outline, big figure, label) but the figure
    counts 0 -> value over the scene. Returns an RGBA numpy array;
    the caller builds the MoviePy mask from the alpha channel.
    """
    import numpy as np
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=36,
                           fill=CARD_BG, outline=TEAL, width=4)
    parsed = parse_stat_number(big)
    if parsed is not None:
        p = _ease_out(t / max(duration, 0.01))
        shown = parsed["value"] * p
        val = f"{shown:.0f}" if float(parsed["value"]).is_integer() else f"{shown:.1f}"
        big_text = f"{parsed['prefix']}{val}{parsed['suffix']}"
    else:
        big_text = big
    size = 150
    font = _load_font(size, bold=True)
    while size > 60 and draw.textlength(big_text, font=font) > width - 80:
        size -= 10
        font = _load_font(size, bold=True)
    bbox = draw.textbbox((0, 0), big_text, font=font)
    big_h = bbox[3] - bbox[1]
    _text_centred(draw, width / 2, 64, big_text, font, (255, 255, 255, 255))
    label_font = _load_font(46, bold=True)
    from tools.image_gen import _wrap_by_width

    lines = _wrap_by_width(draw, label, label_font, width - 100)[:2]
    y = 64 + big_h + 28
    for line in lines:
        _text_centred(draw, width / 2, y, line, label_font, (190, 220, 210, 255))
        y += 58
    return np.asarray(img)
