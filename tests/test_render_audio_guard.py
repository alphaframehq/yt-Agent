"""Real-render regression tests for the run #23 audio-overrun fix.

Run #23 (27 Nov 2026, 'Anthropic IPO: $81 Billion') failed at
render/finalize at 88% with:
    Error in file .../cache/media/9b63ae376e2182f37307549e32b12135.mp3,
    Accessing time t=147.25-147.29 seconds, with clip duration=147 seconds

Root cause: the background music is a single 147.226s Pixabay track.
_render_composition() did
``AudioFileClip(music).volumex(0.08).set_duration(video.duration)``,
which only changes the DECLARED duration — the file is still
147.226s. MoviePy's FFMPEG_AudioReader raises as soon as a whole
requested audio chunk starts past the file end, so any video longer
than the music track crashed at the tail. Shorts (30-60s) never hit
it because the music outlasted them.

The fix (agents/assemble.py): never stretch an audio track past its
own file — loop a short music track to fill the video, trim a long
one, always end the music AUDIO_END_MARGIN_SEC before the video end;
trim scene narration longer than its scene the same way.

These tests render REAL videos with moviepy (no fakes — the bug is
in real MoviePy reader behaviour, so fakes cannot catch it). They
are small (2 scenes x 1.5s) to keep CI fast. Skipped when moviepy
or ffmpeg is unavailable.
"""
from __future__ import annotations

import math
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

# Local-only compat (Python 3.12 dev VMs): Pillow >= 10 removed
# Image.ANTIALIAS, which moviepy 1.0.3 still references. CI runs
# Python 3.11 with pinned Pillow==9.5.0 where ANTIALIAS exists, so
# this branch never fires there.
try:
    from PIL import Image as _Image

    if not hasattr(_Image, "ANTIALIAS"):
        _Image.ANTIALIAS = _Image.Resampling.LANCZOS
except Exception:
    pass

try:
    import moviepy  # noqa: F401  (proves the real renderer is present)

    _HAS_MOVIEPY = True
except Exception:
    _HAS_MOVIEPY = False

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _make_wav(path: Path, seconds: float, rate: int = 24000, freq: float = 440.0):
    n = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(
            b"".join(
                struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
                for i in range(n)
            )
        )


def _make_png(path: Path, color):
    from PIL import Image

    Image.new("RGB", (320, 180), color).save(path)


def _make_mp3(path: Path, seconds: float):
    wav = path.with_suffix(".wav")
    _make_wav(wav, seconds, rate=44100, freq=220.0)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(wav),
         "-codec:a", "libmp3lame", "-q:a", "4", str(path)],
        check=True,
    )
    wav.unlink()


def _probe_duration_sec(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


@unittest.skipUnless(_HAS_MOVIEPY and _HAS_FFMPEG,
                     "needs real moviepy + ffmpeg for a real render")
class TestRenderAudioGuard(unittest.TestCase):
    """Audio-overrun guard: music/scene audio must never be read past
    the end of its source file (run #23)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.imgs = []
        for i, color in enumerate([(200, 40, 40), (40, 200, 40)]):
            p = self.dir / f"scene{i}.png"
            _make_png(p, color)
            self.imgs.append(str(p))
        self.srt = self.dir / "subtitles.srt"
        self.srt.write_text(
            "1\n00:00:00,000 --> 00:00:01,500\nScene one\n\n"
            "2\n00:00:01,500 --> 00:00:03,000\nScene two\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _render(self, durations, audio_secs, music_sec=None, dest_name="render.mp4"):
        from agents.assemble import VideoAssembler

        audios = []
        for i, secs in enumerate(audio_secs):
            p = self.dir / f"audio{i}.wav"
            _make_wav(p, secs)
            audios.append(p)
        audio_assets = [
            {"duration_sec": d, "audio_path": str(a)}
            for d, a in zip(durations, audios)
        ]
        visual_assets = [
            {"asset_path": p, "media_type": "image", "visual_type": "ai_image"}
            for p in self.imgs
        ]
        music = None
        if music_sec is not None:
            music = self.dir / "music.mp3"
            _make_mp3(music, music_sec)
            music = str(music)
        dest = self.dir / dest_name
        assembler = VideoAssembler(is_shorts=False)
        out = assembler._render_composition(
            dest, audio_assets, visual_assets, music, self.srt
        )
        self.assertEqual(out, str(dest.resolve()))
        self.assertTrue(dest.exists())
        self.assertGreater(dest.stat().st_size, 1024)
        return dest

    def test_short_music_is_looped_not_stretched(self):
        """Run #23, scaled down: 2.4s video (2 x 1.5s scenes minus the
        0.6s Phase-3 cross-dissolve overlap), 2.0s music. Before the
        fix this raised 'Accessing time t=2.0x, clip duration=2'."""
        dest = self._render(
            durations=(1.5, 1.5), audio_secs=(1.5, 1.5),
            music_sec=2.0, dest_name="render_short_music.mp4",
        )
        self.assertAlmostEqual(_probe_duration_sec(dest), 2.4, delta=0.2)

    def test_long_music_is_trimmed(self):
        """5.0s music under a 2.4s video: trimmed, renders fine."""
        dest = self._render(
            durations=(1.5, 1.5), audio_secs=(1.5, 1.5),
            music_sec=5.0, dest_name="render_long_music.mp4",
        )
        self.assertAlmostEqual(_probe_duration_sec(dest), 2.4, delta=0.2)

    def test_scene_audio_longer_than_scene_is_trimmed(self):
        """2.0s narration files declared as 1.5s scenes: the audio
        must be trimmed to the scene, not bleed past the video."""
        dest = self._render(
            durations=(1.5, 1.5), audio_secs=(2.0, 2.0),
            music_sec=None, dest_name="render_long_audio.mp4",
        )
        self.assertAlmostEqual(_probe_duration_sec(dest), 2.4, delta=0.2)

    def test_scene_audio_shorter_than_scene_renders(self):
        """Declared durations rounded up 2 decimals from the WAV
        frames can exceed the file by a few ms (dur 1.51 vs a 1.5s
        file): must still render, with silence in the tail gap."""
        dest = self._render(
            durations=(1.51, 1.51), audio_secs=(1.5, 1.5),
            music_sec=None, dest_name="render_short_audio.mp4",
        )
        self.assertAlmostEqual(_probe_duration_sec(dest), 2.42, delta=0.2)

    def test_no_music_renders(self):
        """Baseline: no background music at all still renders."""
        dest = self._render(
            durations=(1.5, 1.5), audio_secs=(1.5, 1.5),
            music_sec=None, dest_name="render_no_music.mp4",
        )
        self.assertAlmostEqual(_probe_duration_sec(dest), 2.4, delta=0.2)


if __name__ == "__main__":
    unittest.main()
