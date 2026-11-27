"""Guard test (27 Nov 2026, run #20): gemini-2.5-flash-lite is a
retired model — it returns HTTP 404 on the free tier and killed run
#20 in the Shorts fallback chain. It must never reappear as a live
model id in any fallback chain.

(27 Nov 2026, run #24): moonshotai/kimi-k3 is retired on Bluesminds
— it returns HTTP 410 (gone); the Shorts fallback chain fell through
to OpenRouter. Same rule: it must never reappear as a live model id.

Comments noting the removal are allowed; a live use is not. The
TTS model "gemini-2.5-flash-preview-tts" is a different, live model
and must NOT be flagged by this guard.
"""

import unittest
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_RETIRED = ("gemini-2.5-flash-lite", "moonshotai/kimi-k3")


class TestNoRetiredModels(unittest.TestCase):
    def test_free_tier_text_models_excludes_retired(self):
        from tools.llm_provider import (
            FREE_TIER_TEXT_MODELS,
            FREE_TIER_TTS_MODELS,
        )

        for retired in _RETIRED:
            self.assertNotIn(retired, FREE_TIER_TEXT_MODELS)
        # Guard must not overreach: the TTS preview model is live.
        self.assertIn("gemini-2.5-flash-preview-tts", FREE_TIER_TTS_MODELS)

    def test_verify_chain_excludes_retired(self):
        from config_manager import DEFAULT_CONFIG

        models = [m for _, m in DEFAULT_CONFIG.get("verify_chain", [])]
        for retired in _RETIRED:
            self.assertNotIn(retired, models)

    def test_no_retired_model_id_in_code(self):
        """No non-comment line in the pipeline code may name a
        retired model as a model id."""
        bad = []
        roots = [_ROOT / d for d in
                 ("tools", "agents", "models", "orchestration", "scripts")]
        files = [p for r in roots for p in r.rglob("*.py")]
        files.append(_ROOT / "config_manager.py")
        for path in files:
            for i, line in enumerate(
                path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
            ):
                if line.strip().startswith("#"):
                    continue
                for retired in _RETIRED:
                    if retired in line:
                        bad.append(
                            f"{path.relative_to(_ROOT)}:{i}: {line.strip()[:120]}"
                        )
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
