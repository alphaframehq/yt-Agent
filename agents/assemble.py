# LOCAL PRODUCTION PATCH — 2026-11-25 — Applied via PR #1 (branch fix/production-mocks), 2026-11-25.
# Source: alphaframehq/yt-Agent @ main c39f42541b8e784dcf41c09583dc57227be07667
# Fixes audit P0 #11 (listed as #12 in the task slice): _render_composition()
# now RAISES the MoviePy/FFmpeg rendering error instead of writing a
# ~40-byte empty MP4 as success. The placeholder-MP4 success path is
# removed from production: _generate_placeholder_mp4() raises unless an
# explicit ALLOW_DEMO_MODE env flag is set (default off), and even then
# writes a distinctly named *.DEMO-PLACEHOLDER.mp4 file, never the real
# destination name.
# MOTION PASS (Phase 2, 26 Nov 2026): the P1 gaps noted below are now
# implemented — Ken Burns zoom/pan on stills (_ken_burns_clip), chart
# fade-in (CHART_FADEIN_SEC), cross-dissolve transitions between
# scenes (CROSSFADE_SEC), and burned-in subtitles via a second FFmpeg
# pass (_burn_subtitles, best-effort with a loud warning on failure)
# — subtitles now opt-in only (YT_BURN_SUBTITLES=1), Phase 3.
# MOTION PASS (Phase 3, 28 Nov 2026 — Dev: "jo bol raha hai, usko
# screen par visuals me samjhaye"): narration subtitles are OFF by
# default for BOTH formats (opt-in only via YT_BURN_SUBTITLES=1, for
# accessibility); scenes are explained, not just moved — a
# narration-derived animated explainer visual (agents/explain.py:
# chat bubbles / wallet + coins / count-up stat / comparison bars /
# labelled diagram, Pillow + MoviePy make_frame) replaces a still
# whenever the scene has no real stock video and no chart; Ken Burns
# is stronger (1.30x) with a beat change mid-scene and vertical /
# diagonal pans; scenes join with a true cross-dissolve (overlapping
# CompositeVideoClip + crossfadein, audio re-timed to the shifted
# scene starts); charts draw in left-to-right instead of fading in;
# Shorts stat cards count up. Dip-to-black is gone.
# "Motion" here means zooms/pans/transitions/animated charts and
# explainer animation only — there is no real-motion presenter video
# in this pipeline (AI video generation is region-blocked for this
# account); never claim otherwise.
# "Motion" here means zooms/pans/transitions/animated charts only —
# there is no real-motion presenter video in this pipeline (AI video
# generation is region-blocked for this account); never claim otherwise.
"""Video Assembly Engine for TubePilot AI.

Combines:
- Scene visual clips (VideoClips or ImageClips with Ken Burns zoom effect)
- Chart PNG overlays with alpha fade-in
- Scene voiceover narration (AWS Polly Neural)
- Ambient background music at volume 0.08
- Burned-in subtitles (SRT via faster-whisper / FFmpeg subtitles filter)
  — OPT-IN ONLY via YT_BURN_SUBTITLES=1 (accessibility); default OFF
  for both formats, 28 Nov 2026
- Supports both 16:9 Longform (1920x1080) and 9:16 Shorts (1080x1920)

Motion (implemented 26 Nov 2026, Phase 2; reworked 28 Nov 2026,
Phase 3): every still image gets a deterministic Ken Burns zoom/pan
(no static image sits still), chart scenes draw in left-to-right,
scenes join with a cross-dissolve, and explanatory animated visuals
(agents/explain.py) illustrate what the voiceover is saying. The
generated SRT is burned in ONLY when the caller opts in with
``YT_BURN_SUBTITLES=1`` (accessibility); by default both formats
ship WITHOUT burned narration subtitles — the voice speaks, the
screen shows visuals. If the opt-in burn fails (missing
filter/font/ffmpeg), the render is still delivered WITHOUT burned
subtitles and a clear warning is logged — the failure is never
silent, and ``VideoAssembler.subtitles_burned`` reports the truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger

from agents.tts import synthesize_script_audio
from agents.visuals import research_bundle_to_chart_data, resolve_scene_visuals
from models.script import ShortsScript, VideoScript
from tools.pixabay import download_media, search_background_music

__all__ = [
    "VideoAssembler",
    "assemble_video",
    "assemble_shorts",
    "_ken_burns_clip",
]

OUTPUT_DIR = Path("cache/renders")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# A successfully rendered video must be at least this large. The old
# placeholder was a ~40-byte ftyp atom; anything near that size is not
# a real render.
MIN_RENDER_BYTES = 1024

# --- Motion constants (Phase 2, 26 Nov 2026; Phase 3, 28 Nov 2026) ---
# Ken Burns: slow zoom between 1.0x and this factor over the scene.
# Phase 3 raised it 1.12 -> 1.30: at 1.12x a 10s still barely moves
# on a phone screen, and Dev judged the visuals "static". The zoom
# now also reverses direction at two beat points inside the scene
# (see _ken_burns_clip), so the eye sees a change mid-scene.
KEN_BURNS_MAX_ZOOM = 1.30
# Cross-dissolve between scenes: each scene after the first starts
# this long BEFORE the previous one ends and crossfades in over it
# (see _render_composition). Total video length is therefore
# sum(scene durations) - CROSSFADE_SEC * (n_scenes - 1), and scene
# starts (SRT, audio) shift by the same amount — see _generate_srt.
CROSSFADE_SEC = 0.6
# First scene only: a short fade-in from black at the very start.
OPEN_FADE_SEC = 0.3
# Chart PNG overlays draw in over the scene (left-to-right reveal,
# _chart_build_clip); CHART_FADEIN_SEC is kept as the first-scene
# fade-in floor for chart scenes so a chart never pops in hard.
CHART_FADEIN_SEC = 0.8
# Subtitle style burned in by FFmpeg (libass). DejaVu Sans ships with
# most ffmpeg installs; if the filter/font is missing the burn is
# skipped with a warning (see _burn_subtitles), never a crash.
SUBTITLE_STYLE = (
    "FontName=DejaVu Sans,FontSize=22,PrimaryColour=&HFFFFFF&,"
    "OutlineColour=&H000000&,BorderStyle=1,Outline=2,Alignment=2,MarginV=40"
)
AUDIO_END_MARGIN_SEC = 0.1
# Audio safety margin (run #23, 27 Nov 2026): every composed audio
# track is ended this far BEFORE the video end. The background music
# is a single 147.226s Pixabay track; a long video longer than that
# crashed the render at the tail because set_duration() stretched the
# music clip past its own file, and MoviePy's FFMPEG_AudioReader
# raises ("Accessing time t=... with clip duration=...") as soon as
# a whole requested audio chunk starts past the file end. Scene
# narration durations are also rounded to 2 decimals from the WAV
# frames, so a scene file can be a few ms shorter than its declared
# duration — the same margin keeps those reads in range too.


def _demo_mode_enabled() -> bool:
    """True only when the caller explicitly opts into demo placeholders.

    Default off. Production must never set ALLOW_DEMO_MODE.
    """
    return os.environ.get("ALLOW_DEMO_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _scene_start(idx: int, durations: List[float]) -> float:
    """Start time of scene ``idx`` in the cross-dissolved timeline:
    the plain cumulative start minus one CROSSFADE_SEC overlap per
    earlier scene boundary. Scene 0 always starts at 0."""
    return max(0.0, sum(durations[:idx]) - CROSSFADE_SEC * idx)


def _beat_zoom(p: float, zoom_in: bool) -> float:
    """Zoom factor at progress ``p`` (0..1) of a scene, with a beat
    change inside the scene: the zoom runs to its extreme at the
    first third, pulls back part-way at the second third, then runs
    to the extreme again — so a long scene visibly transforms twice
    instead of drifting in one direction for its whole length."""
    zoom = KEN_BURNS_MAX_ZOOM
    if zoom_in:
        points = [(0.0, 1.0), (1 / 3, zoom), (2 / 3, 1.0 + (zoom - 1.0) * 0.4), (1.0, zoom)]
    else:
        points = [(0.0, zoom), (1 / 3, 1.0), (2 / 3, 1.0 + (zoom - 1.0) * 0.6), (1.0, 1.0)]
    for (p0, z0), (p1, z1) in zip(points, points[1:]):
        if p <= p1:
            f = 0.0 if p1 == p0 else (p - p0) / (p1 - p0)
            return z0 + (z1 - z0) * f
    return points[-1][1]


def _beat_pan(p: float) -> float:
    """Pan progress 0..1 at scene progress ``p``: ping-pong across
    the scene's three beats (0 -> 1 -> 0 -> 1) so the pan direction
    reverses twice, in sync with the zoom beats."""
    if p < 1 / 3:
        return p * 3
    if p < 2 / 3:
        return 1 - (p - 1 / 3) * 3
    return (p - 2 / 3) * 3


def _ken_burns_clip(
    image_path: str,
    duration: float,
    width: int = 1920,
    height: int = 1080,
    variant: int = 0,
    cover: bool = True,
) -> Any:
    """Still image with a deterministic Ken Burns zoom/pan.

    No extra API calls, no randomness: ``variant`` (the scene index)
    picks one of eight deterministic moves — zoom-in/out x pan
    horizontal / vertical / diagonal, forward / backward — so the
    same script always renders the same motion. Implemented as a
    time-varying MoviePy ``resize`` wrapped in a fixed-size
    ``CompositeVideoClip`` (the composite crops the oversized frame
    to the canvas), with a time-varying position for the pan. Every
    still therefore moves for its whole duration; no static image
    sits longer than a scene.

    Phase 3 (28 Nov 2026): the zoom range is 1.0–1.30x (was 1.12x),
    and both zoom and pan reverse at two beat points inside the
    scene (``_beat_zoom`` / ``_beat_pan``) — a long scene now visibly
    transforms mid-scene instead of drifting one way throughout.

    ``cover`` (Shorts quality fix F2): when True the image is first
    scaled by ``max(width/iw, height/ih)`` so it COVER-fills the
    canvas — a landscape 1280x720 photo in a 1080x1920 Short no
    longer sits as a 37.5% strip over black. The Ken Burns zoom
    (1.0–1.30x) is applied on top of the cover scale, and the pan
    span is computed from the real scaled image size, so the frame
    stays full at every t. Charts pass ``cover=False``: their PNGs
    are 1800x900 (2:1) and cover-cropping would cut axis labels.
    """
    from moviepy.editor import CompositeVideoClip, ImageClip

    zoom_in = variant % 2 == 0

    def _zoom(t: float) -> float:
        return _beat_zoom(t / duration if duration else 0.0, zoom_in)

    base = ImageClip(image_path).set_duration(duration)
    iw, ih = getattr(base, "size", (width, height)) or (width, height)
    cover_scale = max(width / iw, height / ih) if cover and iw and ih else 1.0

    def _scale(t: float) -> float:
        return cover_scale * _zoom(t)

    zoomed = base.resize(_scale)
    # Pan axis from the variant (8-move cycle): 0,1 horizontal;
    # 2,3 vertical; 4-7 diagonal (both axes). Direction flips every
    # two variants.
    cycle = variant % 8
    pan_forward = (variant // 2) % 2 == 0

    def _pos(t: float) -> tuple:
        s = _scale(t)
        span_x = width - iw * s  # <= 0: how far the frame may travel
        span_y = height - ih * s
        q = _beat_pan(t / duration if duration else 0.0)
        if not pan_forward:
            q = 1 - q
        pan_x = cycle in (0, 1, 4, 5, 6, 7)
        pan_y = cycle in (2, 3, 4, 5, 6, 7)
        x = span_x * q if pan_x else span_x / 2
        y = span_y * q if pan_y else span_y / 2
        return (x, y)

    moving = zoomed.set_position(_pos)
    # Opaque background (bg_color=(0, 0, 0)), NOT the default
    # transparent composite. MoviePy 1.0.3 builds a mask for a
    # transparent CompositeVideoClip from each child's mask, and a
    # child with no mask gets add_mask() — whose mask is a static
    # all-ones array of the clip's .size (evaluated once, at t=0).
    # But this clip's frame GROWS with the time-varying zoom, so on
    # zoom-in variants the frozen mask ends up shorter than the
    # frame and, because _pos centres the growing image by moving
    # it up (y = span_y/2 < 0), the mask leaves an uncovered black
    # strip at the bottom that grows with t (measured in the v2
    # sample: 6 rows at t=0.5s -> 108 rows at t=8.5s). The strip
    # only shows once an outer transparent composite applies the
    # mask — i.e. as soon as the Shorts stat card is composited
    # over the scene. With an opaque background the clip has no
    # mask at all, so outer composites fall back to a full-canvas
    # all-ones mask and the frames stay full. The background never
    # shows: the image cover-fills the canvas at every t.
    return CompositeVideoClip(
        [moving], size=(width, height), bg_color=(0, 0, 0)
    ).set_duration(duration)


def _subtitles_opted_in() -> bool:
    """True only when the caller explicitly opts into burned-in
    narration subtitles (accessibility). Default OFF for both
    formats (Dev, 28 Nov 2026): the voice speaks, the screen shows
    visuals — never the spoken words as text."""
    return os.environ.get("YT_BURN_SUBTITLES", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _explainer_clip(narration: str, duration: float, width: int, height: int) -> Any:
    """Animated explanatory visual for one narration (Phase 3).

    Derives the visual beat from the narration (agents/explain.py)
    and renders it as a MoviePy VideoClip whose frames are drawn by
    Pillow at time ``t`` — chat bubbles sliding in, coins flying
    into a wallet, a number counting up, comparison bars growing,
    diagram boxes connecting. Raises when MoviePy's VideoClip is
    unavailable (unit-test fakes) or the beat is ``generic`` — the
    caller falls back to the Ken Burns still in both cases.
    """
    from moviepy.editor import VideoClip

    from agents.explain import derive_visual_beat, draw_explainer_frame

    beat = derive_visual_beat(narration)
    if beat.get("kind") == "generic":
        raise ValueError("no explanatory beat for this narration")
    return VideoClip(
        lambda t: draw_explainer_frame(t, beat, duration, width, height),
        duration=duration,
    )


def _chart_build_clip(png_path: str, duration: float, width: int, height: int) -> Any:
    """Chart PNG that BUILDS in left-to-right instead of fading in
    (Phase 3). A teal pen line sweeps across while the chart is
    revealed behind it, so a chart scene shows a drawing being made
    rather than a static image appearing. Raises when MoviePy's
    VideoClip is unavailable — the caller falls back to Ken Burns +
    fade-in (the old chart treatment)."""
    import numpy as np
    from PIL import Image

    from moviepy.editor import VideoClip

    chart = Image.open(png_path).convert("RGB")
    cw, ch = chart.size
    scale = min(width / cw, height / ch)
    nw, nh = max(1, int(round(cw * scale))), max(1, int(round(ch * scale)))
    chart = chart.resize((nw, nh), Image.LANCZOS)
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    canvas[:] = (14, 17, 23)
    ox, oy = (width - nw) // 2, (height - nh) // 2
    canvas[oy:oy + nh, ox:ox + nw] = np.asarray(chart)
    bg = np.zeros((height, width, 3), dtype=np.uint8)
    bg[:] = (14, 17, 23)

    def _frame(t: float):
        p = max(0.0, min(1.0, t / duration if duration else 1.0))
        # Ease-out reveal; the pen line sits at the reveal edge.
        p = 1.0 - (1.0 - p) ** 2
        reveal_x = int(ox + nw * p)
        frame = bg.copy()
        if reveal_x > ox:
            frame[:, ox:reveal_x] = canvas[:, ox:reveal_x]
        if 0 < p < 1:
            x = max(ox, min(width - 3, reveal_x))
            frame[:, x:x + 3] = (0, 212, 170)
        return frame

    return VideoClip(_frame, duration=duration)


def _animated_stat_clip(
    big: str, label: str, duration: float, width: int, height: int
) -> Any:
    """Stat card whose figure COUNTS UP 0 -> value over the scene
    (Phase 3). Same card art as the static PNG, rendered per-frame
    by Pillow with an explicit mask clip built from the alpha
    channel. Returns None (caller falls back to the static card)
    when the figure cannot count up (word figures like "Millions")
    or MoviePy's VideoClip is unavailable (unit-test fakes)."""
    try:
        from moviepy.editor import VideoClip

        from agents.explain import draw_stat_card_frame, parse_stat_number

        if parse_stat_number(big) is None:
            return None

        def _rgb(t: float):
            return draw_stat_card_frame(t, big, label, duration)[:, :, :3]

        def _mask(t: float):
            return draw_stat_card_frame(t, big, label, duration)[:, :, 3].astype(float) / 255.0

        clip = VideoClip(_rgb, duration=duration)
        mask = VideoClip(_mask, ismask=True, duration=duration)
        return clip.set_mask(mask).set_position(("center", "center"))
    except Exception:  # noqa: BLE001 — decoration: fall back to static card
        return None


def _escape_subtitles_path(path: str) -> str:
    """Escape a file path for FFmpeg's ``subtitles`` filter value.

    The filter parses ``:`` and ``'`` specially, so backslashes,
    colons and single quotes are escaped. (Windows drive-letter
    colons are the classic failure this guards against.)
    """
    return (
        path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    )


# --- Shorts on-screen text (Dev, 27 Nov 2026) -------------------------
# NEVER write the narration on screen for Shorts ("jo bol rha usko
# likhna nhi") — no narration subtitles at all. What the voice says
# is shown as VISUALS + STATS: one big stat/number card per scene
# plus a hook title for the first 2 s. All overlays are rendered
# with Pillow (no TextClip — ImageMagick is absent on the runner).
_STAT_NUMBER_RE = re.compile(
    r"\$?\s?(\d+(?:\.\d+)?)\s*(billion|million|trillion)\b", re.IGNORECASE
)
_STAT_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_STAT_WORD_RE = re.compile(r"\b(millions?|billions?|trillions?)\b", re.IGNORECASE)
_STAT_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "with",
    "and", "or", "but", "is", "are", "was", "were", "be", "been",
    "by", "from", "as", "it", "its", "this", "that", "these", "those",
    "can", "could", "will", "would", "just", "now", "over", "into",
    "before", "after", "remains", "remain", "officially", "making",
}
_STAT_ACRONYMS = {"ai": "AI", "ipo": "IPO", "usd": "USD", "ceo": "CEO"}


def _titlecase_stat(text: str) -> str:
    """Title-case a stat label, keeping known acronyms upper-case."""
    return " ".join(
        _STAT_ACRONYMS.get(w.lower(), w.capitalize()) for w in text.split()
    )


def _content_words(text: str) -> list:
    """Words carrying meaning — stopwords dropped, order kept."""
    return [
        w for w in re.findall(r"[A-Za-z][A-Za-z'\-]*", str(text or ""))
        if w.lower() not in _STAT_STOPWORDS
    ]


def _extract_stat(narration: str) -> tuple:
    """Extract one (big, label) stat from a scene narration.

    Big is the key figure: ``$81B`` / ``42%`` / ``Millions``.
    Label is a 2–4 word caption from the same scene (the clause
    after the figure, else the words after it). With no figure at
    all the first two content words become the big text
    (e.g. ``BIG QUESTION``) and the next four the label. Never
    returns empty strings — a scene always gets a stat card.
    """
    text = str(narration or "").strip()
    if not text:
        return ("KEY FACT", "What Matters")

    def _label_after(match, pattern_text: str) -> str:
        clauses = [c for c in re.split(r"[,.!?;:]", text) if c.strip()]
        idx = next(
            (i for i, c in enumerate(clauses) if pattern_text in c), 0
        )
        words: list = []
        if idx + 1 < len(clauses):
            words = _content_words(clauses[idx + 1])
        if len(words) < 2:
            words = _content_words(text[match.end():]) or _content_words(
                clauses[idx]
            )
        return _titlecase_stat(" ".join(words[:4])) or "Key Figure"

    m = _STAT_NUMBER_RE.search(text)
    if m:
        num = m.group(1)
        suffix = {"billion": "B", "million": "M", "trillion": "T"}.get(
            (m.group(2) or "").lower(), ""
        )
        return (f"${num}{suffix}", _label_after(m, m.group(0)))
    m = _STAT_PCT_RE.search(text)
    if m:
        return (f"{m.group(1)}%", _label_after(m, m.group(0)))
    m = _STAT_WORD_RE.search(text)
    if m:
        return (m.group(1).capitalize(), _label_after(m, m.group(1)))
    words = _content_words(text)
    big = " ".join(words[:2]).upper() or "KEY FACT"
    label = _titlecase_stat(" ".join(words[2:6])) or "What Matters"
    return (big, label)


def _render_stat_card(big: str, label: str, dest_path: Path) -> Path:
    """Render one stat card as a transparent PNG (920x480).

    Big figure ~150 px bold, label ~46 px below it, on a
    translucent dark rounded card with a teal outline. The big text
    shrinks until it fits the card — it can never overflow it.
    """
    from PIL import Image, ImageDraw

    from tools.image_gen import _load_font, _wrap_by_width

    card_w, card_h = 920, 480
    img = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [0, 0, card_w - 1, card_h - 1],
        radius=36,
        fill=(10, 14, 20, 165),
        outline=(0, 212, 170, 255),
        width=4,
    )
    big_size = 150
    big_font = _load_font(big_size, bold=True)
    while (
        big_size > 60
        and draw.textlength(big, font=big_font) > card_w - 80
    ):
        big_size -= 10
        big_font = _load_font(big_size, bold=True)
    bbox = draw.textbbox((0, 0), big, font=big_font)
    big_h = bbox[3] - bbox[1]
    draw.text(
        ((card_w - draw.textlength(big, font=big_font)) / 2, 64),
        big,
        font=big_font,
        fill=(255, 255, 255, 255),
    )
    label_font = _load_font(46, bold=True)
    label_lines = _wrap_by_width(draw, label, label_font, card_w - 100)[:2]
    y = 64 + big_h + 28
    for line in label_lines:
        lw = draw.textlength(line, font=label_font)
        draw.text(
            ((card_w - lw) / 2, y),
            line,
            font=label_font,
            fill=(190, 220, 210, 255),
        )
        y += 58
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest_path, "PNG")
    return dest_path


def _stat_card_clip(
    big: str, label: str, duration: float, width: int, height: int
) -> Any:
    """Stat card as a MoviePy clip, centred, for the scene duration.

    Returns None (with a warning) instead of raising when the card
    cannot be rendered — a missing stat card must never kill a render.
    """
    from moviepy.editor import ImageClip

    try:
        key = hashlib.md5(f"{big}|{label}".encode("utf-8")).hexdigest()[:12]
        dest = OUTPUT_DIR / f"stat_{key}.png"
        _render_stat_card(big, label, dest)
        return (
            ImageClip(str(dest))
            .set_duration(duration)
            .set_position(("center", "center"))
        )
    except Exception as exc:  # noqa: BLE001 — stat is decoration
        logger.warning(f"Stat card render failed ({exc}); skipping it.")
        return None


def _hook_text(script: Any) -> str:
    """Hook for the first 2 s: the SEO title shortened, else the
    first 5 words of scene 1's narration."""
    title = ""
    try:
        title = (getattr(getattr(script, "seo", None), "title", "") or "").strip()
    except Exception:  # noqa: BLE001 — fall through to narration
        title = ""
    if title:
        head = re.split(r"\s[-|]\s", title)[0].strip()
        words = head.split()
        if 2 <= len(words) <= 6:
            return head
        return " ".join(words[:5])
    scenes = getattr(script, "scenes", []) or []
    if scenes:
        return " ".join(
            str(getattr(scenes[0], "narration", "") or "").split()[:5]
        )
    return ""


def _render_hook_overlay(hook: str, dest_path: Path, width: int = 1080) -> Path:
    """Render the hook title as a transparent PNG (1080x320).

    ~96 px bold, centred, white with a black stroke so it reads over
    any visual. Shrinks until it fits the frame width.
    """
    from PIL import Image, ImageDraw

    from tools.image_gen import _load_font

    img = Image.new("RGBA", (width, 320), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    size = 96
    font = _load_font(size, bold=True)
    while size > 48 and draw.textlength(hook, font=font) > width - 120:
        size -= 8
        font = _load_font(size, bold=True)
    bbox = draw.textbbox((0, 0), hook, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (width - text_w) / 2 - bbox[0]
    y = 40 - bbox[1]
    draw.text(
        (x, y),
        hook,
        font=font,
        fill=(255, 255, 255, 255),
        stroke_width=4,
        stroke_fill=(0, 0, 0, 255),
    )
    bar_w = min(text_w, width - 160)
    draw.rectangle(
        [(width - bar_w) / 2, y + text_h + 18, (width + bar_w) / 2, y + text_h + 26],
        fill=(0, 212, 170, 255),
    )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest_path, "PNG")
    return dest_path


def _burn_subtitles(video_path: Path, srt_path: Path) -> bool:
    """Burn the SRT at *srt_path* into *video_path* (second FFmpeg pass).

    Renders to a temp file in the same directory and atomically
    replaces the original only on success. Returns True when
    subtitles were actually burned. Returns False (and logs a clear
    warning) when ffmpeg is missing, the ``subtitles`` filter is
    unavailable, or the filter run fails — the caller then delivers
    the render WITHOUT burned subtitles and must say so (see
    ``VideoAssembler.subtitles_burned``). Never raises for a burn
    failure: a missing subtitle burn must not lose a good render.
    """
    if not srt_path.exists():
        logger.warning(
            f"Subtitle burn skipped: SRT file missing ({srt_path}). "
            "Delivering render WITHOUT burned-in subtitles."
        )
        return False
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.warning(
            "Subtitle burn skipped: ffmpeg not found on PATH. "
            "Delivering render WITHOUT burned-in subtitles."
        )
        return False

    filter_arg = (
        f"subtitles='{_escape_subtitles_path(str(srt_path.resolve()))}'"
        f":force_style='{SUBTITLE_STYLE}'"
    )
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(video_path.parent), prefix=video_path.stem + ".subs.", suffix=".mp4"
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        result = subprocess.run(
            [
                ffmpeg, "-y",
                "-i", str(video_path),
                "-vf", filter_arg,
                "-c:a", "copy",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(
            f"Subtitle burn failed to run ({e}). Delivering render "
            "WITHOUT burned-in subtitles."
        )
        tmp_path.unlink(missing_ok=True)
        return False
    if result.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size < MIN_RENDER_BYTES:
        logger.warning(
            "Subtitle burn FAILED (ffmpeg subtitles filter error; "
            f"stderr: {(result.stderr or '').strip()[-500:] or 'n/a'}). "
            "Delivering render WITHOUT burned-in subtitles — check "
            "that ffmpeg was built with libass and a usable font."
        )
        tmp_path.unlink(missing_ok=True)
        return False
    os.replace(tmp_path, video_path)
    logger.info(f"Burned-in subtitles applied to {video_path}")
    return True


class VideoAssembler:
    """Multi-track video and audio compositor."""

    def __init__(self, is_shorts: bool = False):
        self.is_shorts = is_shorts
        self.resolution = (1080, 1920) if is_shorts else (1920, 1080)
        self.fps = 30
        # True only when the last render actually burned subtitles in.
        # Read this after assemble() — never assume subtitles are
        # burned just because an SRT file was generated.
        self.subtitles_burned = False

    def assemble(
        self,
        script: VideoScript | ShortsScript,
        output_filename: Optional[str] = None,
        bundle_data: Optional[Any] = None,
    ) -> str:
        """Compose all scenes, attach audio, background music, and render MP4.

        Motion applied per scene: Ken Burns zoom/pan on stills (or an
        animated explainer visual when the narration derives one),
        chart draw-in on chart scenes, cross-dissolve transitions
        between scenes. Subtitles are burned ONLY when the caller
        opted in with YT_BURN_SUBTITLES=1; by default the render has
        no burned narration text at all. If the burn fails, the render
        is still returned but ``self.subtitles_burned`` stays False
        and a warning is logged.

        bundle_data: a ResearchBundle, or a chart-data dict as built by
        research_bundle_to_chart_data(). Chart scenes REQUIRE it — if it
        is None, generate_chart_by_type() raises instead of plotting
        fabricated data. Non-chart scenes do not need it.
        """
        title_str = getattr(script, "topic", getattr(script.seo, "title", "video"))
        dest_name = output_filename or f"render_{'shorts_' if self.is_shorts else 'long_'}{title_str.lower().replace(' ', '_')[:20]}.mp4"
        dest_path = OUTPUT_DIR / dest_name

        logger.info(f"Starting video assembly for '{title_str}' -> {dest_path}")

        # 1. Synthesize audio per scene
        audio_assets = synthesize_script_audio(script)

        # 2. Resolve visuals per scene — with real research data for charts.
        if bundle_data is not None and not isinstance(bundle_data, dict):
            bundle_data = research_bundle_to_chart_data(bundle_data)
        visual_assets = resolve_scene_visuals(
            script, bundle_data=bundle_data, is_shorts=self.is_shorts
        )

        # 3. Locate background music
        bg_tracks = search_background_music()
        bg_music_path = download_media(bg_tracks[0]["url"], extension="mp3") if bg_tracks else None

        # 4. Subtitles: OFF by default for BOTH formats (Dev,
        # 28 Nov 2026) — the voice speaks, the screen shows visuals,
        # never the spoken words. Only an explicit YT_BURN_SUBTITLES=1
        # (accessibility opt-in) generates + burns an SRT for the
        # LONG video; Shorts never get narration subtitles.
        srt_path = (
            self._generate_srt(audio_assets, script)
            if (not self.is_shorts and _subtitles_opted_in())
            else None
        )
        stat_texts = (
            [getattr(s, "narration", "") for s in script.scenes]
            if self.is_shorts
            else None
        )
        narration_texts = [getattr(s, "narration", "") for s in script.scenes]
        hook_text = _hook_text(script) if self.is_shorts else ""

        # 5. Composite scenes via MoviePy / FFmpeg. Failures raise —
        # no placeholder MP4 is written as success in production.
        return self._render_composition(
            dest_path,
            audio_assets,
            visual_assets,
            bg_music_path,
            srt_path,
            stat_texts=stat_texts,
            hook_text=hook_text,
            narration_texts=narration_texts,
        )

    def _generate_srt(self, audio_assets: List[Dict[str, Any]], script: Any) -> Path:
        """Generate time-aligned SRT subtitle file.

        Timings are cross-dissolve aware: scene ``idx`` starts at
        ``_scene_start(idx, durations)``, i.e. one CROSSFADE_SEC
        earlier per earlier boundary, matching the rendered video.
        Only used when subtitles are opted in (YT_BURN_SUBTITLES=1).
        """
        srt_file = OUTPUT_DIR / "subtitles.srt"
        durations = [asset["duration_sec"] for asset in audio_assets]
        lines = []

        for idx, (asset, scene) in enumerate(zip(audio_assets, script.scenes)):
            dur = asset["duration_sec"]
            start = _scene_start(idx, durations)
            t_start = self._format_timestamp(start)
            t_end = self._format_timestamp(start + dur)
            lines.append(f"{idx + 1}\n{t_start} --> {t_end}\n{scene.narration}\n")

        srt_file.write_text("\n".join(lines), encoding="utf-8")
        return srt_file

    def _format_timestamp(self, seconds: float) -> str:
        """Format seconds to SRT time format: 00:00:00,000."""
        hrs = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int((seconds - int(seconds)) * 1000)
        return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"

    def _render_composition(
        self,
        dest_path: Path,
        audio_assets: List[Dict[str, Any]],
        visual_assets: List[Dict[str, Any]],
        bg_music_path: Optional[str],
        srt_path: Optional[Path],
        stat_texts: Optional[List[str]] = None,
        hook_text: str = "",
        narration_texts: Optional[List[str]] = None,
    ) -> str:
        """Execute MoviePy / FFmpeg rendering pipeline.

        Raises on any MoviePy/FFmpeg failure. Motion (Phase 3, 28 Nov
        2026): still images get a stronger deterministic Ken Burns
        zoom/pan (1.0–1.30x, beat change mid-scene, variant = scene
        index); chart scenes DRAW IN left-to-right; image scenes
        whose narration derives an explanatory beat get an animated
        explainer visual instead of the still (agents/explain.py);
        scenes join with a true CROSS-DISSOLVE — each scene after
        the first starts CROSSFADE_SEC before the previous ends and
        crossfades in over it, so cuts are never hard and never dip
        to black. Video and audio are built as separate tracks on
        the same shifted timeline (scene ``idx`` starts at
        ``_scene_start(idx, durations)``), so narration stays in
        sync with its visuals and the total duration is
        sum(durations) - CROSSFADE_SEC * (n - 1). After the render,
        an SRT is burned in ONLY when one was passed (opt-in
        YT_BURN_SUBTITLES=1); if that burn fails, the render is
        still delivered but ``self.subtitles_burned`` stays False and
        a warning is logged — never a silent claim of subtitles.
        Background music, if missing, is silently omitted (pre-existing
        behaviour, kept unchanged by this patch).
        """
        if not audio_assets or not visual_assets:
            raise ValueError("Cannot render: no audio or visual assets to compose.")
        try:
            # Check MoviePy availability
            from moviepy.editor import (
                AudioFileClip,
                CompositeAudioClip,
                CompositeVideoClip,
                ImageClip,
                VideoFileClip,
            )

            width, height = self.resolution
            durations = [a["duration_sec"] for a in audio_assets]
            total_duration = (
                sum(durations) - CROSSFADE_SEC * (len(durations) - 1)
                if len(durations) > 1
                else sum(durations)
            )
            video_clips = []
            audio_clips = []
            for idx, (a_info, v_info) in enumerate(zip(audio_assets, visual_assets)):
                dur = a_info["duration_sec"]
                v_path = v_info["asset_path"]
                is_chart = v_info.get("visual_type") == "chart"
                start = _scene_start(idx, durations)
                narration = (
                    narration_texts[idx]
                    if narration_texts and idx < len(narration_texts)
                    else ""
                )
                explainer_used = False

                if v_info["media_type"] == "video" and Path(v_path).exists():
                    # Cover-fill (F2): resize + center-crop to exactly
                    # the canvas, so a landscape stock clip can never
                    # shrink the whole Short to a 2:3 frame.
                    clip = VideoFileClip(v_path).subclip(0, min(dur, 10)).set_duration(dur)
                    vw, vh = getattr(clip, "size", (width, height)) or (width, height)
                    vscale = max(width / vw, height / vh) if vw and vh else 1.0
                    nw, nh = int(round(vw * vscale)), int(round(vh * vscale))
                    clip = clip.resize((nw, nh)).crop(
                        x_center=nw // 2, y_center=nh // 2,
                        width=width, height=height,
                    )
                elif is_chart:
                    # Chart scenes draw in left-to-right (Phase 3);
                    # fall back to Ken Burns + fade-in when the
                    # animated clip cannot be built (unit-test fakes).
                    try:
                        clip = _chart_build_clip(v_path, dur, width, height)
                    except Exception:  # noqa: BLE001 — fallback below
                        clip = _ken_burns_clip(v_path, dur, width, height, variant=idx)
                else:
                    # Phase 3: explain the narration when we can —
                    # an animated explainer visual (chat / wallet /
                    # stat / comparison / diagram) replaces the still
                    # whenever the scene has no real stock video and
                    # no chart. Falls back to the Ken Burns still for
                    # generic beats or when VideoClip is unavailable.
                    try:
                        clip = _explainer_clip(narration, dur, width, height)
                        explainer_used = True
                    except Exception:  # noqa: BLE001 — Ken Burns fallback
                        clip = _ken_burns_clip(v_path, dur, width, height, variant=idx)

                # Shorts stat card (F1): one big figure + label per
                # scene, centred over the visual — counting up when
                # the figure is numeric (Phase 3). Never the
                # narration text — Dev, 27 Nov 2026. Skipped when
                # the explainer already carries the scene's figure
                # (stat / wallet beats), so the number is not shown
                # twice.
                if self.is_shorts and stat_texts and idx < len(stat_texts) and not explainer_used:
                    big, label = _extract_stat(stat_texts[idx])
                    stat_clip = _animated_stat_clip(big, label, dur, width, height)
                    if stat_clip is None:
                        stat_clip = _stat_card_clip(big, label, dur, width, height)
                    if stat_clip is not None:
                        clip = CompositeVideoClip(
                            [clip, stat_clip], size=(width, height)
                        ).set_duration(dur)

                # Transitions: the first scene fades in from black
                # (chart scenes over CHART_FADEIN_SEC, so a chart
                # never pops in hard); every later scene
                # cross-dissolves in over the previous one's tail.
                # A single fadein/crossfadein call only — stacking
                # two corrupts the mask.
                if idx == 0:
                    fade_in = max(CHART_FADEIN_SEC, OPEN_FADE_SEC) if is_chart else OPEN_FADE_SEC
                    clip = clip.fadein(fade_in)
                else:
                    clip = clip.crossfadein(CROSSFADE_SEC)
                clip = clip.set_start(start).set_duration(dur)
                video_clips.append(clip)

                # Scene voiceover audio, trimmed to the scene length
                # and placed at the same shifted start as its visual.
                # A file longer than dur would bleed past the scene
                # (and, on the last scene, past the video, so the
                # composed audio would outlast the video). A file a
                # few ms shorter than dur (durations are rounded to
                # 2 decimals from the WAV frames) simply leaves
                # silence in the tail gap — CompositeAudioClip skips
                # clips that are not playing at a given time.
                audio_clip = AudioFileClip(a_info["audio_path"])
                if audio_clip.duration is not None and audio_clip.duration > dur:
                    audio_clip = audio_clip.subclip(0, dur)
                audio_clips.append(audio_clip.set_start(start))

            final_video = CompositeVideoClip(
                video_clips, size=(width, height)
            ).set_duration(total_duration)

            # Background music, mixed with the narration track. The
            # music track must never be stretched past its own
            # length: set_duration() on a shorter file only changes
            # the DECLARED duration, and MoviePy raises ("Accessing
            # time t=... with clip duration=...") as soon as a whole
            # requested audio chunk starts past the file end (run
            # #23: the 147.226s Pixabay track vs a ~147.29s video).
            # So a short track is looped to fill the video, a long
            # one is trimmed, and the music always ends
            # AUDIO_END_MARGIN_SEC before the video end. A degenerate
            # (<= margin) video gets no music at all.
            music_clips = []
            if bg_music_path and Path(bg_music_path).exists():
                bg_audio = AudioFileClip(bg_music_path).volumex(0.08)
                music_target = total_duration - AUDIO_END_MARGIN_SEC
                if music_target > 0:
                    if bg_audio.duration is None:
                        # Unknown length: cannot loop or trim it
                        # safely, so leave it out rather than risk
                        # reading past the end of the file.
                        logger.warning(
                            "Background music duration is unknown; "
                            "skipping it rather than risk an overrun."
                        )
                    else:
                        if bg_audio.duration < music_target:
                            # Imported lazily: only the loop path needs
                            # it, and importing moviepy.audio.fx at
                            # the top of this function breaks the
                            # fake-moviepy unit tests (their stub
                            # module has no moviepy.audio submodule).
                            from moviepy.audio.fx.all import audio_loop

                            logger.info(
                                f"Background music ({bg_audio.duration:.1f}s) "
                                f"is shorter than the video "
                                f"({total_duration:.1f}s); looping it "
                                f"to {music_target:.1f}s."
                            )
                            bg_audio = audio_loop(bg_audio, duration=music_target)
                        else:
                            bg_audio = bg_audio.subclip(0, music_target)
                        music_clips.append(bg_audio)
                else:
                    logger.warning(
                        "Video is shorter than the audio safety margin "
                        f"({AUDIO_END_MARGIN_SEC}s); skipping background "
                        "music."
                    )
            final_video = final_video.set_audio(
                CompositeAudioClip(audio_clips + music_clips)
            )

            # Hook title overlay (F4, Shorts only): the hook at the
            # top for the first 2 s, then it fades out. Pillow PNG —
            # no TextClip / ImageMagick. Audio is preserved across
            # the composite.
            if self.is_shorts and hook_text:
                try:
                    hook_path = _render_hook_overlay(
                        hook_text, OUTPUT_DIR / "hook_overlay.png", width
                    )
                    hook_clip = (
                        ImageClip(str(hook_path))
                        .set_start(0)
                        .set_duration(min(2.0, final_video.duration))
                        .fadeout(0.5)
                        .set_position(("center", 100))
                    )
                    hook_audio = final_video.audio
                    final_video = CompositeVideoClip(
                        [final_video, hook_clip], size=(width, height)
                    ).set_duration(final_video.duration)
                    if hook_audio is not None:
                        final_video = final_video.set_audio(hook_audio)
                except Exception as exc:  # noqa: BLE001 — hook is decoration
                    logger.warning(f"Hook overlay failed ({exc}); skipping it.")

            # Cover-fill guarantee (F2): the finished frame must be
            # exactly the canvas — never a 2:3 or letterboxed output.
            # Real clips always carry .size; fake/stub clips in unit
            # tests do not, and are skipped rather than failed here
            # (the real v2 sample render asserts the size on disk).
            final_size = getattr(final_video, "size", None)
            if final_size is not None and tuple(final_size) != (width, height):
                raise RuntimeError(
                    f"Render size mismatch: got {tuple(final_size)}, "
                    f"expected {(width, height)}."
                )

            final_video.write_videofile(
                str(dest_path.resolve()),
                fps=self.fps,
                codec="libx264",
                audio_codec="aac",
                preset="ultrafast",
            )
            logger.info(f"MoviePy assembly complete: {dest_path}")

            # Validate the render is a real file, not an empty container.
            if not dest_path.exists() or dest_path.stat().st_size < MIN_RENDER_BYTES:
                raise RuntimeError(
                    f"Render produced no usable video at {dest_path} "
                    f"(missing or < {MIN_RENDER_BYTES} bytes)."
                )

            # Burn in subtitles (second FFmpeg pass) — ONLY when an
            # SRT was passed, i.e. the YT_BURN_SUBTITLES=1 opt-in.
            # Shorts never get narration subtitles (Dev, 27 Nov 2026)
            # and the long video does not by default either (Dev,
            # 28 Nov 2026): srt_path is None unless opted in.
            # Best-effort: a failed burn keeps the good render, sets
            # the flag False, and logs a warning — never a silent claim.
            if srt_path is not None:
                self.subtitles_burned = _burn_subtitles(dest_path, srt_path)
                if not self.subtitles_burned:
                    logger.warning(
                        f"Render {dest_path} delivered WITHOUT burned-in "
                        "subtitles (burn failed or unavailable). The SRT "
                        f"file still exists at {srt_path} for manual use."
                    )
            else:
                self.subtitles_burned = False
            return str(dest_path.resolve())

        except Exception as e:
            # Production behaviour: fail loudly. A demo placeholder is
            # only allowed behind the explicit ALLOW_DEMO_MODE flag
            # (default off) and uses a distinct filename.
            if _demo_mode_enabled():
                logger.warning(
                    f"MoviePy rendering failed ({e}). ALLOW_DEMO_MODE is set: "
                    "writing a distinctly named demo placeholder instead."
                )
                return self._generate_placeholder_mp4(dest_path)
            logger.error(f"MoviePy/FFmpeg rendering failed ({e}). Raising instead of writing a placeholder MP4.")
            raise RuntimeError(f"Video rendering failed: {e}") from e

    def _generate_placeholder_mp4(self, dest_path: Path) -> str:
        """Demo-only: create a minimal MP4 container.

        Raises unless ALLOW_DEMO_MODE is explicitly enabled. Even in
        demo mode it writes a DISTINCT filename
        (<stem>.DEMO-PLACEHOLDER.mp4), never the real destination, so a
        placeholder can never be mistaken for a successful render.
        The file is a ~40-byte ftyp atom with no video/audio frames.
        """
        if not _demo_mode_enabled():
            raise RuntimeError(
                "Placeholder MP4 rendering is disabled: set ALLOW_DEMO_MODE=1 "
                "explicitly for local dev only. Production rendering must "
                "raise the MoviePy/FFmpeg error instead of writing an "
                "empty MP4 as success."
            )
        demo_path = dest_path.with_name(f"{dest_path.stem}.DEMO-PLACEHOLDER.mp4")
        # Standard minimal MP4 ftyp atom
        mp4_ftyp_atom = bytes.fromhex(
            "00000018667479706d7034320000000069736f6d6d703432"
            "0000000866726565"
            "000000106d6461740000000000000000"
        )
        demo_path.write_bytes(mp4_ftyp_atom)
        logger.warning(f"DEMO MODE: placeholder video render written to: {demo_path} (not a real render).")
        return str(demo_path.resolve())


def assemble_video(script: VideoScript, output_filename: Optional[str] = None, bundle_data: Optional[Any] = None) -> str:
    """High-level function to assemble a longform 16:9 documentary.

    bundle_data: ResearchBundle (or chart-data dict) for chart scenes —
    required for chart scenes, otherwise they raise instead of plotting
    fabricated data.
    """
    return VideoAssembler(is_shorts=False).assemble(script, output_filename, bundle_data=bundle_data)


def assemble_shorts(shorts_script: ShortsScript, output_filename: Optional[str] = None, bundle_data: Optional[Any] = None) -> str:
    """High-level function to assemble a vertical 9:16 YouTube Short."""
    return VideoAssembler(is_shorts=True).assemble(shorts_script, output_filename, bundle_data=bundle_data)
