# LOCAL PRODUCTION PATCH — 2026-11-25 — Applied via PR #1 (branch fix/production-mocks), 2026-11-25.
# Source: alphaframehq/yt-Agent @ main c39f42541b8e784dcf41c09583dc57227be07667
# Fixes audit P0 #11 (listed as #12 in the task slice): _render_composition()
# now RAISES the MoviePy/FFmpeg rendering error instead of writing a
# ~40-byte empty MP4 as success. The placeholder-MP4 success path is
# removed from production: _generate_placeholder_mp4() raises unless an
# explicit ALLOW_DEMO_MODE env flag is set (default off), and even then
# writes a distinctly named *.DEMO-PLACEHOLDER.mp4 file, never the real
# destination name.
# NOTE (P1, unimplemented claims — not invented here): the module docstring
# promises burned-in subtitles, Ken Burns zoom and chart fade-in, but the
# original code implements none of them: the SRT file is generated but
# never burned into the video (no subtitles filter is applied in
# write_videofile), ImageClip is used with no Ken Burns zoom, and chart
# PNGs get no fade-in. Do not claim those features until implemented.
"""Video Assembly Engine for TubePilot AI.

Combines:
- Scene visual clips (VideoClips or ImageClips with Ken Burns zoom effect)
- Chart PNG overlays with alpha fade-in
- Scene voiceover narration (AWS Polly Neural)
- Ambient background music at volume 0.08
- Burned-in subtitles (SRT via faster-whisper / FFmpeg subtitles filter)
- Supports both 16:9 Longform (1920x1080) and 9:16 Shorts (1080x1920)

P1 CAVEAT (2026-11-25 patch note): burned-in subtitles, Ken Burns zoom
and chart fade-in are claimed above but NOT implemented in this file —
the SRT is generated but never burned in, ImageClip has no zoom applied,
and charts have no fade-in. See _render_composition().
"""

from __future__ import annotations

import json
import os
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
]

OUTPUT_DIR = Path("cache/renders")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# A successfully rendered video must be at least this large. The old
# placeholder was a ~40-byte ftyp atom; anything near that size is not
# a real render.
MIN_RENDER_BYTES = 1024


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


class VideoAssembler:
    """Multi-track video and audio compositor."""

    def __init__(self, is_shorts: bool = False):
        self.is_shorts = is_shorts
        self.resolution = (1080, 1920) if is_shorts else (1920, 1080)
        self.fps = 30

    def assemble(
        self,
        script: VideoScript | ShortsScript,
        output_filename: Optional[str] = None,
        bundle_data: Optional[Any] = None,
    ) -> str:
        """Compose all scenes, attach audio, background music, burned subtitles, and render MP4.

        NOTE: "burned subtitles" in this docstring is aspirational — the
        SRT file is generated (step 4) but is NOT burned into the rendered
        video by _render_composition() (no subtitles filter is applied).

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
        visual_assets = resolve_scene_visuals(script, bundle_data=bundle_data)

        # 3. Locate background music
        bg_tracks = search_background_music()
        bg_music_path = download_media(bg_tracks[0]["url"], extension="mp3") if bg_tracks else None

        # 4. Generate subtitle SRT manifest
        srt_path = self._generate_srt(audio_assets, script)

        # 5. Composite scenes via MoviePy / FFmpeg. Failures raise —
        # no placeholder MP4 is written as success in production.
        return self._render_composition(dest_path, audio_assets, visual_assets, bg_music_path, srt_path)

    def _generate_srt(self, audio_assets: List[Dict[str, Any]], script: Any) -> Path:
        """Generate time-aligned SRT subtitle file."""
        srt_file = OUTPUT_DIR / "subtitles.srt"
        current_sec = 0.0
        lines = []

        for idx, (asset, scene) in enumerate(zip(audio_assets, script.scenes), start=1):
            dur = asset["duration_sec"]
            t_start = self._format_timestamp(current_sec)
            t_end = self._format_timestamp(current_sec + dur)
            current_sec += dur

            lines.append(f"{idx}\n{t_start} --> {t_end}\n{scene.narration}\n")

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
        srt_path: Path,
    ) -> str:
        """Execute MoviePy / FFmpeg rendering pipeline.

        Raises on any MoviePy/FFmpeg failure. P1 note: burned-in
        subtitles are NOT applied here (the SRT at *srt_path* is only
        generated, never passed to a subtitles filter), ImageClips get
        NO Ken Burns zoom, and chart overlays get NO fade-in — those
        features are unimplemented and must not be claimed as present.
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
                ImageClip,
                VideoFileClip,
                concatenate_videoclips,
            )

            clips = []
            for a_info, v_info in zip(audio_assets, visual_assets):
                dur = a_info["duration_sec"]
                v_path = v_info["asset_path"]

                if v_info["media_type"] == "video" and Path(v_path).exists():
                    clip = VideoFileClip(v_path).subclip(0, min(dur, 10)).set_duration(dur)
                else:
                    clip = ImageClip(v_path).set_duration(dur)

                # Attach scene voiceover audio
                audio_clip = AudioFileClip(a_info["audio_path"])
                clip = clip.set_audio(audio_clip)
                clips.append(clip)

            final_video = concatenate_videoclips(clips, method="compose")

            # Add background music if present
            if bg_music_path and Path(bg_music_path).exists():
                bg_audio = AudioFileClip(bg_music_path).volumex(0.08).set_duration(final_video.duration)
                final_audio = CompositeAudioClip([final_video.audio, bg_audio])
                final_video = final_video.set_audio(final_audio)

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
