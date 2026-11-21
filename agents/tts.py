"""Text-to-Speech (TTS) Agent for TubePilot AI.

Synthesizes high-fidelity voiceover using AWS Polly Neural voices:
- Joanna (US English, Female)
- Matthew (US English, Male)
- Kajal (Indian English, Female)
With automated fallback to Standard Polly voices.

Feeds accurate audio durations back into the scene timing model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple
from loguru import logger

from config_manager import get_config
from models.script import SceneInfo, VideoScript
from tools.polly import synthesize_speech

__all__ = [
    "TTSAgent",
    "synthesize_scene_audio",
    "synthesize_script_audio",
]


class TTSAgent:
    """Orchestrates scene-by-scene audio narration synthesis and timing alignment."""

    def __init__(self, voice: Optional[str] = None):
        self.voice = voice or get_config("voice") or "Joanna"

    def synthesize_scene(self, scene: SceneInfo) -> Tuple[str, float]:
        """Synthesize a single scene's narration text and return (audio_path, duration_seconds)."""
        logger.info(f"Synthesizing audio for Scene {scene.id} ({len(scene.narration.split())} words)")
        audio_path, duration = synthesize_speech(
            text=scene.narration,
            voice=self.voice,
            engine="neural",
        )
        return audio_path, duration

    def synthesize_script(self, script: VideoScript) -> List[Dict[str, Any]]:
        """Synthesize audio for every scene in the script and update scene durations."""
        results: List[Dict[str, Any]] = []
        total_audio_sec = 0.0

        for scene in script.scenes:
            audio_path, duration = self.synthesize_scene(scene)
            # Update scene duration with exact spoken audio duration
            scene.duration_sec = int(round(duration))
            total_audio_sec += duration

            results.append({
                "scene_id": scene.id,
                "audio_path": audio_path,
                "duration_sec": duration,
                "section": scene.section,
            })

        logger.info(f"Script TTS synthesis completed. Total duration: {total_audio_sec:.1f}s across {len(results)} scenes.")
        return results


def synthesize_scene_audio(text: str, voice: str = "Joanna") -> Tuple[str, float]:
    """Convenience functional interface for scene audio synthesis."""
    return synthesize_speech(text, voice=voice)


def synthesize_script_audio(script: VideoScript, voice: Optional[str] = None) -> List[Dict[str, Any]]:
    """Convenience functional interface for script audio synthesis."""
    return TTSAgent(voice=voice).synthesize_script(script)
