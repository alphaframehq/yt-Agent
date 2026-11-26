"""Regression tests for the 26 Nov 2026 pipeline fixes + AI-niche pivot.

Covers (original brief):
- empty/missing yt_config -> DEFAULT_CONFIG (incl. malformed rows)
- Gemini safety refusal -> SafetyBlockedError, never retried / never
  routed to the next fallback provider
- empty queue -> dev_run exits 0 ("queue empty, nothing to do")
- bad queued topic -> marked failed in the Sheet, next topic tried
- topic-safety/niche filter (blocklist + AI-niche allowlist)
- YouTube transient-only retry predicate

Covers (26 Nov 2026 niche correction, pillar ai_everything):
- ai_everything is the default pillar everywhere (dev_run, pipeline,
  graph, ResearchInput) and is accepted by the Pydantic Literals
- AI topics pass the filter; pure-finance / gossip / viral topics do not
- the AI research path needs no ticker (TinyFish + YouTube only)
- prompts/script_ai_everything.txt exists and matches the verify
  section set; pillar_split is an AI-angles mix summing to 1.0

No network, no real Sheets, no secrets: Sheets is the in-memory
fake (tests/_sheets_fake.py), HTTP is mocked, LLM keys are fakes.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests._sheets_fake import install_fake_sheets

_FAKE_SHEETS = None
_SHEETS_PATCHER = None


def setUpModule():
    global _FAKE_SHEETS, _SHEETS_PATCHER
    _FAKE_SHEETS, _SHEETS_PATCHER = install_fake_sheets()


def tearDownModule():
    if _SHEETS_PATCHER is not None:
        _SHEETS_PATCHER.stop()


import config_manager
from config_manager import DEFAULT_CONFIG, snapshot_config
from tools.topic_filter import check_topic
from tools.llm_provider import SafetyBlockedError, call_with_fallback, LLMProvider
from tools import secrets as secrets_mod
from workflow.errors import ErrorClass, classify_error
from tools.youtube_api import _is_transient_youtube_error
from db.sheets import (
    enqueue_topic,
    get_next_topic,
    list_topics_queue,
    mark_topic_failed,
)
from orchestration.graph import TubePilotGraph


def _queue_row(topic_id, topic, pillar="ai_everything", status="pending",
               priority=1, added_at=1000):
    return [topic_id, topic, pillar, status, priority, added_at]


class TestTopicFilterAINiche(unittest.TestCase):
    """AI niche: allow anything new in AI; block explicit + off-niche."""

    def test_ai_topics_allowed(self):
        allowed_topics = [
            "New ChatGPT agent can book your flights",
            "How LLMs Are Trained",
            "AI scam: fake Elon Musk deepfake crypto giveaway",
            "GPT-5 leak: what we know so far",
            "AI predictions for 2027",
            "Best AI tools for video editing",
            "Mistral's new open model beats GPT-4",
            "The AI conspiracy: is it hiding something?",
            "How neural networks learn: the backprop formula",
            "Will AI replace programmers? Pros and cons",
            "Google DeepMind's new robot can fold laundry",
        ]
        for topic in allowed_topics:
            ok, reason = check_topic(topic)
            self.assertTrue(ok, f"{topic!r} should be allowed, got {reason}")

    def test_run13_topic_blocked(self):
        ok, reason = check_topic("best poses for sex")
        self.assertFalse(ok)
        self.assertIn("blocked-term:sex", reason)

    def test_explicit_beats_ai_signal(self):
        # "sexy" is blocked even though the topic is about an AI app.
        ok, reason = check_topic("Sexy AI girlfriend apps exposed")
        self.assertFalse(ok)
        self.assertIn("blocked-term", reason)

    def test_off_niche_rejected(self):
        off_niche = [
            "How Stripe Makes Money",
            "How Revolut Makes Money",
            "Kylie Jenner new boyfriend drama",
            "Top 10 viral dance challenges",
            "Apple stock price prediction",
            "Stripe business model breakdown",
        ]
        for topic in off_niche:
            ok, reason = check_topic(topic)
            self.assertFalse(ok, f"{topic!r} should be off-niche, got {reason}")
            self.assertIn("off-niche", reason)

    def test_sussex_not_a_blocked_term(self):
        # "Sussex" contains "sex" as a substring, not a word — the
        # blocklist must not trip on it (it is off-niche here for
        # having no AI signal, not blocked as explicit).
        ok, reason = check_topic("Sussex county news roundup")
        self.assertFalse(ok)
        self.assertIn("off-niche", reason)

    def test_empty_topic_rejected(self):
        self.assertFalse(check_topic("")[0])
        self.assertFalse(check_topic(None)[0])


class TestPillarDefaults(unittest.TestCase):
    """ai_everything is the default pillar; legacy pillars still accepted."""

    def test_research_input_default_is_ai(self):
        from agents.research import ResearchInput

        self.assertEqual(ResearchInput(topic="How LLMs Are Trained").pillar,
                         "ai_everything")

    def test_pipeline_defaults_are_ai(self):
        import inspect
        from pipeline import run_pipeline as pipeline_run
        from orchestration.graph import run_pipeline as graph_run

        self.assertEqual(
            inspect.signature(pipeline_run).parameters["pillar"].default,
            "ai_everything")
        self.assertEqual(
            inspect.signature(graph_run).parameters["pillar"].default,
            "ai_everything")

    def test_models_accept_ai_pillar(self):
        from models.research import ResearchBundle
        from tests._fixtures import make_test_bundle

        bundle = make_test_bundle()
        # model_validate (not plain assignment) proves the Literal
        # accepts ai_everything.
        revalidated = ResearchBundle.model_validate(
            {**bundle.model_dump(), "pillar": "ai_everything"})
        self.assertEqual(revalidated.pillar, "ai_everything")

    def test_pillar_split_is_ai_mix(self):
        split = DEFAULT_CONFIG["pillar_split"]
        self.assertNotIn("finance", split)
        self.assertAlmostEqual(sum(split.values()), 1.0)
        self.assertIn("news_tools", split)
        self.assertIn("scams_risks", split)

    def test_ai_script_prompt_exists(self):
        path = Path("prompts/script_ai_everything.txt")
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        for section in ("what_changed", "how_it_works", "why_it_matters",
                        "risks_and_scams", "predictions", "disclaimer", "cta"):
            self.assertIn(section, text)
        self.assertIn("UNVERIFIED", text)  # leaks labelled unverified
        self.assertIn("scam", text.lower())


class TestEmptyConfig(unittest.TestCase):
    """Empty / malformed yt_config must never crash a run."""

    def setUp(self):
        config_manager._invalidate_cache()

    def test_empty_config_tab_returns_defaults(self):
        cfg = snapshot_config()
        self.assertEqual(cfg, DEFAULT_CONFIG)

    def test_malformed_config_row_keeps_default(self):
        # Hand-edited cell: active_models stored as a plain string.
        _FAKE_SHEETS.store["yt_config"].append(
            ["active_models", '"not-a-dict"'])
        config_manager._invalidate_cache()
        cfg = snapshot_config()
        self.assertEqual(cfg["active_models"], DEFAULT_CONFIG["active_models"])
        # Other defaults intact; validation passes.
        config_manager.validate_config(cfg)


class TestSafetyRefusal(unittest.TestCase):
    """Gemini SAFETY block -> SafetyBlockedError, no retry, no fallback."""

    def setUp(self):
        os.environ["GEMINI_KEY"] = "AIza" + "x" * 35
        secrets_mod.reset_gemini_pool()

    def tearDown(self):
        os.environ.pop("GEMINI_KEY", None)
        secrets_mod.reset_gemini_pool()

    def _fake_client_class(self, payload):
        class _FakeResp:
            status_code = 200

            def __init__(self):
                self.request = None

            def json(self):
                return payload

            def raise_for_status(self):
                return None

        class _FakeClient:
            calls = 0

            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, url, headers=None, json=None):
                type(self).calls += 1
                return _FakeResp()

        return _FakeClient

    def test_safety_block_raises(self):
        fake = self._fake_client_class(
            {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []})
        with patch("tools.llm_provider.httpx.Client", fake):
            with self.assertRaises(SafetyBlockedError):
                LLMProvider("gemini", "gemini-3.8-flash").call("sys", "user")

    def test_safety_block_not_routed_to_next_provider(self):
        fake = self._fake_client_class(
            {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []})
        with patch("tools.llm_provider.httpx.Client", fake):
            with self.assertRaises(SafetyBlockedError):
                call_with_fallback(
                    [["gemini", "gemini-3.8-flash"],
                     ["openrouter", "openrouter/free"]],
                    "sys", "user")
        # Only the Gemini call happened — OpenRouter was never tried.
        self.assertEqual(fake.calls, 1)

    def test_empty_response_without_block_is_loud_error(self):
        fake = self._fake_client_class({"candidates": []})
        with patch("tools.llm_provider.httpx.Client", fake):
            with self.assertRaises(ValueError):
                LLMProvider("gemini", "gemini-3.8-flash").call("sys", "user")

    def test_safety_classified_fatal_not_retried(self):
        self.assertEqual(
            classify_error(SafetyBlockedError("Gemini safety refusal")),
            ErrorClass.FATAL)


class TestQueueHandling(unittest.TestCase):
    """Empty queue exits 0; bad topics are marked failed and skipped."""

    def _run_dev_main(self, argv):
        import scripts.dev_run as dev_run

        with patch.object(sys, "argv", argv):
            with patch.object(dev_run, "notify_pipeline_failure",
                              return_value=False):
                return dev_run.main()

    def test_empty_queue_exits_zero(self):
        import scripts.dev_run as dev_run

        with patch.object(dev_run, "dequeue_next_topic", return_value=None):
            code = self._run_dev_main(
                ["dev_run.py", "--pillar", "ai_everything", "--no-stop"])
        self.assertEqual(code, 0)

    def test_empty_queue_exits_zero_without_scheduled_flag(self):
        # The daily workflow runs without --scheduled; that must not
        # turn "nothing to do" into exit 2 any more.
        import scripts.dev_run as dev_run

        with patch.object(dev_run, "dequeue_next_topic", return_value=None):
            code = self._run_dev_main(["dev_run.py", "--no-stop"])
        self.assertEqual(code, 0)

    def test_explicit_blocked_topic_exits_two(self):
        code = self._run_dev_main(
            ["dev_run.py", "--topic", "best poses for sex", "--no-stop"])
        self.assertEqual(code, 2)

    def test_bad_queued_topic_marked_failed_then_empty_queue(self):
        # Queue holds ONLY the run #13 topic; the run must mark it
        # failed and then exit 0 on the now-empty queue.
        _FAKE_SHEETS.store["yt_topics_queue"].append(
            _queue_row("top_a63e07a2", "best poses for sex",
                       pillar="finance_to_technical", priority=10))
        code = self._run_dev_main(["dev_run.py", "--no-stop"])
        self.assertEqual(code, 0)
        rows = list_topics_queue()
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIsNone(get_next_topic())

    def test_mark_topic_failed_helper(self):
        top_id = enqueue_topic("New AI tools roundup",
                               pillar="ai_everything", priority=5)
        self.assertTrue(mark_topic_failed(top_id))
        self.assertEqual(
            [t for t in list_topics_queue() if t["topic_id"] == top_id][0]["status"],
            "failed")
        self.assertIsNone(get_next_topic())

    def test_string_priority_does_not_crash_dequeue(self):
        # A hand-edited priority cell reads back as a string ("10").
        _FAKE_SHEETS.store["yt_topics_queue"].append(
            _queue_row("top_str", "AI tools roundup",
                       priority="10", added_at=2000))
        _FAKE_SHEETS.store["yt_topics_queue"].append(
            _queue_row("top_int", "LLM training explained",
                       priority=5, added_at=1000))
        nxt = get_next_topic()
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt["topic_id"], "top_str")


class TestGraphFilterGate(unittest.TestCase):
    """The graph itself rejects bad topics before any research/LLM."""

    def test_blocked_topic_fails_at_filter_stage(self):
        graph = TubePilotGraph()
        state = graph.run(topic="best poses for sex", pillar="ai_everything")
        self.assertEqual(state.status, "failed")
        self.assertEqual(state.stage, "filter")
        self.assertIn("blocked-term", state.error)

    def test_ai_topic_passes_filter_into_research(self):
        graph = TubePilotGraph()
        with patch("agents.research.search_fintech_context",
                   side_effect=RuntimeError("tinyfish-marker")):
            state = graph.run(topic="How LLMs Are Trained",
                              pillar="ai_everything")
        # Past the filter (stage is research, error is the marker from
        # the research attempt — not a filter rejection).
        self.assertEqual(state.stage, "research")
        self.assertEqual(state.error, "tinyfish-marker")


class TestAIResearchPath(unittest.TestCase):
    """ai_everything research needs no ticker (TinyFish + YouTube only)."""

    def test_ai_topic_builds_bundle_without_ticker(self):
        from agents.research import ResearchAgent, ResearchInput

        articles = [
            {"title": "New LLM training breakthrough",
             "source": "Test News", "url": "https://example.test/a",
             "snippet": "Researchers cut training cost by half."},
            {"title": "AI tool of the week",
             "source": "Test News", "url": "https://example.test/b",
             "snippet": "A new video generator ships today."},
        ]
        insights = {"top_titles": ["LLM training explained"],
                    "common_tags": [], "gaps": ["no beginner video"]}
        with patch("agents.research.search_fintech_context",
                   return_value=articles):
            with patch("agents.research.research_topic",
                       return_value=insights):
                bundle = ResearchAgent().run(
                    ResearchInput(topic="How LLMs Are Trained",
                                  pillar="ai_everything"))
        self.assertEqual(bundle.pillar, "ai_everything")
        self.assertEqual(len(bundle.sources), 2)
        self.assertEqual(bundle.financials.growth_rate, 0.0)
        self.assertEqual(bundle.financials.revenue_by_year, {})
        self.assertAlmostEqual(bundle.research_confidence, 0.8)


    def test_validator_still_requires_citation_for_nonzero_margin(self):
        # The 0.0 carve-out must not loosen the rule for real numbers:
        # a non-zero margin without its citation still fails validation.
        from models.research import ResearchBundle
        from tests._fixtures import make_test_bundle

        bundle = make_test_bundle()
        data = bundle.model_dump()
        data["sources"] = [
            s for s in data["sources"] if s["claim_id"] != "margin_gross"
        ]
        with self.assertRaises(ValueError):
            ResearchBundle.model_validate(data)


class TestYoutubeTransientRetry(unittest.TestCase):
    """Retry predicate: transient yes, auth/missing-creds never."""

    def test_transient_markers(self):
        for msg in ("503 Service Unavailable", "429 Rate Limit",
                    "timeout while calling YouTube",
                    "YouTube search API call failed: 500 Internal Error"):
            self.assertTrue(_is_transient_youtube_error(Exception(msg)), msg)

    def test_permanent_markers_never_retried(self):
        for msg in ("invalid_grant: expired refresh token",
                    "YouTube OAuth2 credentials missing: YT_CLIENT_ID "
                    "not set (or empty) in the environment",
                    "HTTP 403 Forbidden",
                    "video not found (404)"):
            self.assertFalse(_is_transient_youtube_error(Exception(msg)), msg)


class TestDailyRunQueueLoop(unittest.TestCase):
    """daily_run must skip poisoned topics like dev_run does (run #13).

    The scheduled daily entry point is scripts/daily_run.py, not
    dev_run.py: a rejected topic used to fail the whole day (exit 1)
    instead of being marked failed and skipped. These tests drive
    daily main() with the fake Sheets store and a mocked run_daily.
    """

    def setUp(self):
        # Reset the queue tab to just its header row (the fake is
        # pre-provisioned with headers; a bare .clear() would also
        # wipe the header and the first appended row would be
        # swallowed as the header).
        from db.sheets import TAB_HEADERS

        _FAKE_SHEETS.store["yt_topics_queue"] = [
            list(TAB_HEADERS["yt_topics_queue"])
        ]

    def tearDown(self):
        # Leave no rows behind: later test classes append rows with
        # the same topic_ids (e.g. the run #13 topic) and assume a
        # clean queue — a leftover "pending" duplicate breaks
        # update-by-first-match in update_topic_status.
        self.setUp()

    def _run_daily_main(self, argv):
        import scripts.daily_run as daily_run

        with patch.object(sys, "argv", argv):
            with patch.object(daily_run, "notify_pipeline_failure",
                              return_value=False):
                return daily_run.main()

    def _ok_summary(self, topic):
        return {"topic": topic, "pillar": "ai_everything",
                "long_status": "complete", "long_video_id": "vid_1",
                "shorts": [], "shorts_summary": {"overall": "skipped"},
                "error": None, "exit_code": 0}

    def test_run13_topic_only_queue_exits_zero_and_marks_failed(self):
        # Queue holds ONLY the run #13 topic; the daily run must mark
        # it failed and then exit 0 on the now-empty queue — never a
        # research ValueError, never exit 1.
        import scripts.daily_run as daily_run

        _FAKE_SHEETS.store["yt_topics_queue"].append(
            _queue_row("top_a63e07a2", "best poses for sex",
                       pillar="finance_to_technical", priority=10))
        with patch.object(daily_run, "run_daily",
                          side_effect=AssertionError("must not run")):
            code = self._run_daily_main(
                ["daily_run.py", "--no-trending", "--no-stop", "--scheduled"])
        self.assertEqual(code, 0)
        rows = list_topics_queue()
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIsNone(get_next_topic())

    def test_bad_topic_skipped_then_good_topic_runs(self):
        import scripts.daily_run as daily_run

        _FAKE_SHEETS.store["yt_topics_queue"].append(
            _queue_row("top_a63e07a2", "best poses for sex",
                       pillar="finance_to_technical", priority=10))
        _FAKE_SHEETS.store["yt_topics_queue"].append(
            _queue_row("top_good1", "New AI coding tools launch",
                       pillar="ai_everything", priority=1))
        with patch.object(daily_run, "run_daily",
                          side_effect=lambda **kw: self._ok_summary(kw["topic"])) as run:
            code = self._run_daily_main(
                ["daily_run.py", "--no-trending", "--no-stop", "--scheduled"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["topic"], "New AI coding tools launch")
        rows = {r["topic_id"]: r for r in list_topics_queue()}
        self.assertEqual(rows["top_a63e07a2"]["status"], "failed")
        self.assertEqual(rows["top_good1"]["status"], "running")

    def test_pipeline_topic_failure_marks_failed_then_next_runs(self):
        # A topic that passes the filter but fails in the pipeline
        # for a topic-attributable reason (Gemini safety refusal)
        # must be marked failed and the next topic tried.
        import scripts.daily_run as daily_run

        _FAKE_SHEETS.store["yt_topics_queue"].append(
            _queue_row("top_first", "AI agents beat benchmarks",
                       pillar="ai_everything", priority=5))
        _FAKE_SHEETS.store["yt_topics_queue"].append(
            _queue_row("top_second", "New AI coding tools launch",
                       pillar="ai_everything", priority=1))
        blocked = {"topic": "AI agents beat benchmarks",
                   "pillar": "ai_everything", "long_status": "failed",
                   "long_video_id": None, "shorts": [],
                   "shorts_summary": {"overall": "skipped"},
                   "error": "prompt was blocked by safety filter",
                   "exit_code": 1}
        with patch.object(daily_run, "run_daily",
                          side_effect=[blocked, self._ok_summary("New AI coding tools launch")]) as run:
            code = self._run_daily_main(
                ["daily_run.py", "--no-trending", "--no-stop", "--scheduled"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 2)
        rows = {r["topic_id"]: r for r in list_topics_queue()}
        self.assertEqual(rows["top_first"]["status"], "failed")
        self.assertEqual(rows["top_second"]["status"], "running")


if __name__ == "__main__":
    unittest.main()
