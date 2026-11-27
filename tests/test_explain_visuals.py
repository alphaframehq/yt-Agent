"""Explanatory visuals (Phase 3, 28 Nov 2026 — Dev: "jo bol raha hai,
usko screen par visuals me samjhaye").

Covers: narration -> visual-beat derivation (chat / wallet / stat /
comparison / diagram / generic), Pillow frame drawing (frames change
over time = real motion, never a static still), stat count-up parsing,
the subtitle opt-in (default OFF for both formats), cross-dissolve
SRT timings, and the stronger Ken Burns curve. Frame tests use real
Pillow + numpy (both installed); no MoviePy is needed for them.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np


class TestDeriveVisualBeat(unittest.TestCase):
    def test_chat_beat_for_agents_deciding(self):
        from agents.explain import derive_visual_beat

        beat = derive_visual_beat(
            "Inside the wallet, AI agents chat in a group, share a plan, "
            "and decide together how to spend money."
        )
        self.assertEqual(beat["kind"], "chat")

    def test_wallet_beat_for_money_moving(self):
        from agents.explain import derive_visual_beat

        beat = derive_visual_beat(
            "The wallet holds real money. When an agent buys a tool, "
            "coins move out of the wallet and the balance drops."
        )
        self.assertEqual(beat["kind"], "wallet")

    def test_stat_beat_for_a_figure(self):
        from agents.explain import derive_visual_beat

        beat = derive_visual_beat(
            "Anthropic just secured over 81 billion dollars in the "
            "largest offering on record."
        )
        self.assertEqual(beat["kind"], "stat")
        self.assertEqual(beat["number"]["text"], "$81B")
        self.assertEqual(beat["number"]["value"], 81.0)

    def test_percent_figure(self):
        from agents.explain import derive_visual_beat

        beat = derive_visual_beat("Revenue grew 42% year over year.")
        self.assertEqual(beat["kind"], "stat")
        self.assertEqual(beat["number"]["suffix"], "%")

    def test_comparison_beat_for_seconds_vs_hours(self):
        from agents.explain import derive_visual_beat

        beat = derive_visual_beat(
            "One agent can now finish in seconds what used to take a "
            "team hours."
        )
        self.assertEqual(beat["kind"], "comparison")
        self.assertEqual(beat["compare_a"], "Seconds")
        self.assertEqual(beat["compare_b"], "Hours")

    def test_diagram_beat_for_a_named_thing(self):
        from agents.explain import derive_visual_beat

        beat = derive_visual_beat(
            "ChatGPT Wallet connects the model, the payment rail and "
            "the merchant in one flow."
        )
        self.assertIn(beat["kind"], ("wallet", "diagram"))

    def test_label_is_never_a_sentence(self):
        """On-screen text is short labels only — the label derived
        from any narration is at most 4 words."""
        from agents.explain import derive_visual_beat

        for narration in (
            "Anthropic just secured over 81 billion dollars in the largest offering on record.",
            "Inside the wallet, AI agents chat in a group, share a plan, and decide together.",
            "The wallet holds real money and every payment is logged.",
        ):
            beat = derive_visual_beat(narration)
            self.assertLessEqual(len(beat["label"].split()), 4)
            self.assertNotIn(narration, beat["label"])

    def test_comparison_outranks_wallet_mention(self):
        """'Finishes in seconds what used to take hours — and every
        payment is logged' is ABOUT the comparison, not the wallet."""
        from agents.explain import derive_visual_beat

        beat = derive_visual_beat(
            "One agent can now finish in seconds what used to take a "
            "whole team hours — and every payment is logged, so you "
            "always see where the money went."
        )
        self.assertEqual(beat["kind"], "comparison")
        self.assertEqual(beat["entity"], "Seconds vs Hours")

    def test_bare_chat_noun_is_not_a_chat_beat(self):
        """'Real money, inside the chat' names the app, not agents
        chatting — the scene is about the wallet."""
        from agents.explain import derive_visual_beat

        beat = derive_visual_beat(
            "ChatGPT gets its own wallet — real money, inside the "
            "chat, ready for agents to spend on tools."
        )
        self.assertEqual(beat["kind"], "wallet")

    def test_entity_prefers_brand_over_sentence_starter(self):
        from agents.explain import derive_visual_beat

        beat = derive_visual_beat(
            "ChatGPT now has its own wallet. Inside it, AI agents "
            "chat in a group and decide together."
        )
        self.assertEqual(beat["kind"], "chat")
        self.assertEqual(beat["entity"], "ChatGPT")

    def test_empty_narration_is_generic_not_crash(self):
        from agents.explain import derive_visual_beat

        beat = derive_visual_beat("")
        self.assertEqual(beat["kind"], "generic")


class TestExplainerFrames(unittest.TestCase):
    def _frames(self, narration, duration=9.0, width=640, height=360):
        from agents.explain import derive_visual_beat, draw_explainer_frame

        beat = derive_visual_beat(narration)
        return beat, [
            draw_explainer_frame(t, beat, duration, width, height)
            for t in (0.5, 4.5, 8.5)
        ]

    def test_frames_are_full_size_rgb(self):
        beat, frames = self._frames(
            "AI agents chat in a group and decide together how to spend money."
        )
        for f in frames:
            self.assertEqual(f.shape, (360, 640, 3))
            self.assertEqual(f.dtype, np.uint8)

    def test_chat_frames_move_over_time(self):
        """Same scene, different times: bubbles slide in, so the
        frames must differ — a static still would be identical."""
        _, frames = self._frames(
            "AI agents chat in a group and decide together how to spend money."
        )
        self.assertFalse(np.array_equal(frames[0], frames[1]))
        self.assertFalse(np.array_equal(frames[1], frames[2]))

    def test_wallet_frames_move_over_time(self):
        _, frames = self._frames(
            "The wallet holds real money. When an agent buys a tool, "
            "coins move out of the wallet."
        )
        self.assertFalse(np.array_equal(frames[0], frames[1]))
        self.assertFalse(np.array_equal(frames[1], frames[2]))

    def test_stat_count_up_grows(self):
        """Stat beat: the bright (white number) pixel count grows as
        the number counts up from ~0 toward its value."""
        from agents.explain import derive_visual_beat, draw_explainer_frame

        beat = derive_visual_beat(
            "Anthropic just secured over 81 billion dollars."
        )
        self.assertEqual(beat["kind"], "stat")
        bright = []
        for t in (0.2, 4.5, 8.8):
            f = draw_explainer_frame(t, beat, 9.0, 640, 360)
            bright.append(int((f.mean(axis=2) > 200).sum()))
        self.assertGreater(bright[2], bright[0])

    def test_chat_tail_keeps_moving(self):
        """After the last bubble is in, the agents still bob — the
        tail of the scene must not freeze (regression: the landscape
        branch once computed the bob offset but drew with the static
        y, so frames 9s vs 10s were bit-identical)."""
        from agents.explain import derive_visual_beat, draw_explainer_frame

        beat = derive_visual_beat(
            "AI agents chat in a group and decide together how to spend money."
        )
        a = draw_explainer_frame(7.2, beat, 9.0, 640, 360)
        b = draw_explainer_frame(8.1, beat, 9.0, 640, 360)
        self.assertFalse(np.array_equal(a, b))

    def test_wallet_tail_keeps_moving(self):
        from agents.explain import derive_visual_beat, draw_explainer_frame

        beat = derive_visual_beat(
            "The wallet holds real money. When an agent buys a tool, "
            "coins move out of the wallet."
        )
        a = draw_explainer_frame(7.2, beat, 9.0, 640, 360)
        b = draw_explainer_frame(8.1, beat, 9.0, 640, 360)
        self.assertFalse(np.array_equal(a, b))

    def test_portrait_frames_too(self):
        beat, frames = self._frames(
            "The wallet holds real money.", duration=6.0, width=360, height=640
        )
        for f in frames:
            self.assertEqual(f.shape, (640, 360, 3))

    def test_every_kind_draws(self):
        from agents.explain import derive_visual_beat, draw_explainer_frame

        narrations = {
            "chat": "AI agents chat in a group and decide together.",
            "wallet": "The wallet holds money and pays for tools.",
            "stat": "Revenue grew 42% year over year.",
            "comparison": "It finishes in seconds what used to take hours.",
            "diagram": "ChatGPT Wallet connects the model and the merchant.",
            "generic": "Something interesting happens here.",
        }
        for kind, narration in narrations.items():
            beat = derive_visual_beat(narration)
            frame = draw_explainer_frame(3.0, beat, 6.0, 640, 360)
            self.assertEqual(frame.shape, (360, 640, 3), msg=kind)
            self.assertGreater(frame.mean(), 5, msg=f"{kind}: frame is black")


class TestStatCountUp(unittest.TestCase):
    def test_parse_stat_number(self):
        from agents.explain import parse_stat_number

        self.assertEqual(parse_stat_number("$81B")["value"], 81.0)
        self.assertEqual(parse_stat_number("42%")["suffix"], "%")
        self.assertEqual(parse_stat_number("$100")["prefix"], "$")
        self.assertIsNone(parse_stat_number("Millions"))
        self.assertIsNone(parse_stat_number("BIG QUESTION"))

    def test_stat_card_frames_count_up(self):
        """Animated stat card: the card is non-transparent, and the
        bright-pixel area of the big figure grows over the scene."""
        from agents.explain import draw_stat_card_frame

        early = draw_stat_card_frame(0.1, "$81B", "Biggest AI IPO", 9.0)
        late = draw_stat_card_frame(8.9, "$81B", "Biggest AI IPO", 9.0)
        self.assertEqual(early.shape, (480, 920, 4))
        self.assertFalse(np.array_equal(early, late))
        # Card is opaque where drawn (alpha > 0 in the middle).
        self.assertGreater(int((early[:, :, 3] > 0).sum()), 100_000)


class TestSubtitleOptIn(unittest.TestCase):
    def test_subtitles_off_by_default(self):
        from agents.assemble import _subtitles_opted_in

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_subtitles_opted_in())

    def test_subtitles_on_with_explicit_flag(self):
        from agents.assemble import _subtitles_opted_in

        with mock.patch.dict(os.environ, {"YT_BURN_SUBTITLES": "1"}):
            self.assertTrue(_subtitles_opted_in())

    def test_srt_timings_are_crossfade_aware(self):
        """Scene 2 of a 5s+5s video starts at 4.4s (5 - 0.6
        cross-dissolve), not 5.0s — the SRT must match the render."""
        import types

        from agents.assemble import VideoAssembler

        assembler = VideoAssembler(is_shorts=False)
        script = types.SimpleNamespace(
            scenes=[
                types.SimpleNamespace(narration="Scene one."),
                types.SimpleNamespace(narration="Scene two."),
            ]
        )
        audio = [
            {"duration_sec": 5.0, "audio_path": "/tmp/a.wav"},
            {"duration_sec": 5.0, "audio_path": "/tmp/a.wav"},
        ]
        srt = assembler._generate_srt(audio, script)
        text = Path(srt).read_text(encoding="utf-8")
        self.assertIn("00:00:00,000 --> 00:00:05,000", text)
        self.assertIn("00:00:04,400 --> 00:00:09,400", text)


if __name__ == "__main__":
    unittest.main()
