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
# fade-in (CHART_FADEIN_SEC), dip transitions between scenes
# (TRANSITION_SEC), and burned-in subtitles via a second FFmpeg pass
# (_burn_subtitles, best-effort with a loud warning on failure).
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
- Supports both 16:9 Longform (1920x1080) and 9:16 Shorts (1080x1920)

Motion (implemented 26 Nov 2026, Phase 2): every still image gets a
deterministic Ken Burns zoom/pan (no static image sits still), chart
scenes fade in over 0.8s, scenes are joined with dip-to-black
transitions, and the generated SRT is burned in with FFmpeg's
``subtitles`` filter in a second pass. If the subtitle burn fails
(missing filter/font/ffmpeg), the render is still delivered WITHOUT
burned subtitles and a clear warning is logged — the failure is never
silent, and ``VideoAssembler.subtitles_burned`` reports the truth.
"""

from __future__ import annotations

import json
import os
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

# --- Motion constants (Phase 2, 26 Nov 2026) -------------------------------
# Ken Burns: slow zoom between 1.0x and this factor over the scene.
KEN_BURNS_MAX_ZOOM = 1.12
# Dip-to-black transitions: fade out/in this long at each scene edge,
# capped at 25% of the scene duration (see _transition_sec).
TRANSITION_SEC = 0.5
# Chart PNG overlays fade in over this long at the scene start.
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


def _transition_sec(duration: float) -> float:
    """Dip-transition length for a scene: 0.5s, capped at 25% of the
    scene so a very short scene never gets a transition longer than
    itself (MoviePy errors on fadein/fadeout > duration)."""
    return min(TRANSITION_SEC, max(0.1, duration * 0.25))


def _ken_burns_clip(
    image_path: str,
    duration: float,
    width: int = 1920,
    height: int = 1080,
    variant: int = 0,
) -> Any:
    """Still image with a deterministic Ken Burns zoom/pan.

    No extra API calls, no randomness: ``variant`` (the scene index)
    picks one of four deterministic moves — zoom-in, zoom-out,
    pan-left, pan-right — so the same script always renders the same
    motion. Implemented as a time-varying MoviePy ``resize`` wrapped
    in a fixed-size ``CompositeVideoClip`` (the composite crops the
    oversized frame to the canvas), with a time-varying position for
    the pan. Every still therefore moves for its whole duration; no
    static image sits longer than a scene.
    """
    from moviepy.editor import CompositeVideoClip, ImageClip

    zoom = KEN_BURNS_MAX_ZOOM
    if variant % 2 == 0:  # zoom in: 1.0x -> 1.12x
        def _scale(t: float) -> float:
            return 1.0 + (zoom - 1.0) * (t / duration)
    else:  # zoom out: 1.12x -> 1.0x
        def _scale(t: float) -> float:
            return zoom - (zoom - 1.0) * (t / duration)

    zoomed = ImageClip(image_path).set_duration(duration).resize(_scale)
    pan_forward = (variant // 2) % 2 == 0

    def _pos(t: float) -> tuple:
        s = _scale(t)
        span_x = width - width * s  # <= 0: how far the frame may travel
        span_y = height - height * s
        p = t / duration
        x = span_x * p if pan_forward else span_x * (1 - p)
        return (x, span_y / 2)

    moving = zoomed.set_position(_pos)
    return CompositeVideoClip([moving], size=(width, height)).set_duration(duration)


def _escape_subtitles_path(path: str) -> str:
    """Escape a file path for FFmpeg's ``subtitles`` filter value.

    The filter parses ``:`` and ``'`` specially, so backslashes,
    colons and single quotes are escaped. (Windows drive-letter
    colons are the classic failure this guards against.)
    """
    return (
        path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    )


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
        """Compose all scenes, attach audio, background music, burned subtitles, and render MP4.

        Motion applied per scene: Ken Burns zoom/pan on stills, 0.8s
        fade-in on chart scenes, dip-to-black transitions between
        scenes. Subtitles are burned in a second FFmpeg pass when
        possible; if the burn fails, the render is still returned but
        ``self.subtitles_burned`` stays False and a warning is logged.

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

        Raises on any MoviePy/FFmpeg failure. Motion (Phase 2):
        still images get a deterministic Ken Burns zoom/pan
        (variant = scene index), chart scenes fade in over
        CHART_FADEIN_SEC, and every scene gets a dip-to-black
        transition (fadein/fadeout) so cuts are never hard. Dip
        transitions are used instead of overlapping crossfades
        deliberately: they keep total duration — and therefore the
        SRT timings generated in step 4 — exact. After the render,
        the SRT is burned in via a second FFmpeg pass
        (:func:`_burn_subtitles`); if that burn fails, the render is
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
                VideoFileClip,
                concatenate_videoclips,
            )

            width, height = self.resolution
            clips = []
            for idx, (a_info, v_info) in enumerate(zip(audio_assets, visual_assets)):
                dur = a_info["duration_sec"]
                v_path = v_info["asset_path"]
                is_chart = v_info.get("visual_type") == "chart"

                if v_info["media_type"] == "video" and Path(v_path).exists():
                    clip = VideoFileClip(v_path).subclip(0, min(dur, 10)).set_duration(dur)
                else:
                    # Still image: Ken Burns zoom/pan (deterministic
                    # variant per scene) so it never sits static.
                    clip = _ken_burns_clip(v_path, dur, width, height, variant=idx)

                # Fade-in: chart scenes fade in over CHART_FADEIN_SEC
                # (the chart overlay fade-in); other scenes fade in over
                # the dip-transition length. A single fadein call only —
                # stacking two fadeins corrupts the mask. Fade-out is
                # always the dip-transition length.
                t_sec = _transition_sec(dur)
                fade_in = max(CHART_FADEIN_SEC, t_sec) if is_chart else t_sec
                clip = clip.fadein(fade_in).fadeout(t_sec)

                # Attach scene voiceover audio, trimmed to the scene
                # length. A file longer than dur would bleed past the
                # scene (and, on the last scene, past the video, so
                # the composed audio would outlast the video). A file
                # a few ms shorter than dur (durations are rounded to
                # 2 decimals from the WAV frames) simply leaves
                # silence in the tail gap — CompositeAudioClip skips
                # clips that are not playing at a given time.
                audio_clip = AudioFileClip(a_info["audio_path"])
                if audio_clip.duration is not None and audio_clip.duration > dur:
                    audio_clip = audio_clip.subclip(0, dur)
                clip = clip.set_audio(audio_clip)
                clips.append(clip)

            final_video = concatenate_videoclips(clips, method="compose")

            # Add background music if present. The music track must
            # never be stretched past its own length: set_duration()
            # on a shorter file only changes the DECLARED duration,
            # and MoviePy raises ("Accessing time t=... with clip
            # duration=...") as soon as a whole requested audio chunk
            # starts past the file end (run #23: the 147.226s Pixabay
            # track vs a ~147.29s video). So a short track is looped
            # to fill the video, a long one is trimmed, and the music
            # always ends AUDIO_END_MARGIN_SEC before the video end.
            # A degenerate (<= margin) video gets no music at all.
            if bg_music_path and Path(bg_music_path).exists():
                bg_audio = AudioFileClip(bg_music_path).volumex(0.08)
                music_target = final_video.duration - AUDIO_END_MARGIN_SEC
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
                                f"({final_video.duration:.1f}s); looping it "
                                f"to {music_target:.1f}s."
                            )
                            bg_audio = audio_loop(bg_audio, duration=music_target)
                        else:
                            bg_audio = bg_audio.subclip(0, music_target)
                        final_audio = CompositeAudioClip([final_video.audio, bg_audio])
                        final_video = final_video.set_audio(final_audio)
                else:
                    logger.warning(
                        "Video is shorter than the audio safety margin "
                        f"({AUDIO_END_MARGIN_SEC}s); skipping background "
                        "music."
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

            # Burn in subtitles (second FFmpeg pass). Best-effort: a
            # failed burn keeps the good render, sets the flag False,
            # and logs a warning — never a silent claim.
            self.subtitles_burned = _burn_subtitles(dest_path, srt_path)
            if not self.subtitles_burned:
                logger.warning(
                    f"Render {dest_path} delivered WITHOUT burned-in "
                    "subtitles (burn failed or unavailable). The SRT "
                    f"file still exists at {srt_path} for manual use."
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
