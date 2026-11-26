"""Unit tests for the pre-script trend-research step (tools/trends.py)
and the pipeline trend node (orchestration/graph.py node_trends).

Trend research is fail-soft by design: feeds are free public RSS
(no API keys), parsing is stdlib-only, and every failure mode must
yield an empty-angles TrendReport — never an exception, never an
invented angle. Tests use fixture RSS bytes and a fake urlopen; no
network is touched.
"""

import calendar
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


from tests._fixtures import make_test_bundle
from orchestration.graph import PipelineState, TubePilotGraph
from tools.trends import (
    DailyTrendPick,
    TrendingTopic,
    fetch_trends,
    parse_frontpage_rss,
    parse_news_rss,
    parse_trends_rss,
    pick_daily_trends,
    topic_from_headline,
    trend_report_for_pick,
)

NEWS_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Google News</title>
<item><title>OpenAI launches new AI agent tool</title>
<link>https://example.com/news/openai-agent</link>
<description>OpenAI said its new agent tool completes multi-step tasks for users.</description></item>
<item><title>AI scam warning: fake video tools steal data</title>
<link>https://example.com/news/ai-scam</link>
<description>Researchers warn fake AI video tools are harvesting user data.</description></item>
</channel></rss>"""

TRENDS_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Daily Search Trends</title>
<item><title>GPT-5 leak</title><link>https://example.com/trends/gpt5-leak</link>
<description>Searches spike after an alleged GPT-5 leak.</description></item>
<item><title>Local football final</title><link>https://example.com/trends/football</link>
<description>A derby match dominates searches.</description></item>
</channel></rss>"""

EMPTY_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Empty</title></channel></rss>"""


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        pass


def _opener_for(news: bytes, trends: bytes):
    def opener(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "news.google.com" in url:
            return _FakeResponse(news)
        return _FakeResponse(trends)

    return opener


def _failing_opener(req, timeout=None):
    raise RuntimeError("network unreachable (fixture)")


# --- Daily picker fixtures (26 Nov 2026, noon UTC) -------------------------
# Same OpenAI story across Google News / HN / Techmeme (must cluster),
# a single-source fresh Nvidia story, a stale Anthropic story (must
# NOT be picked), and non-AI noise (must be filtered out).
_PICK_NOW = calendar.timegm((2026, 11, 26, 12, 0, 0))

GN_PICK_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Google News</title>
<item><title>OpenAI launches GPT-6 agent for coding - The Verge</title>
<link>https://example.com/news/gpt6-agent</link>
<description>OpenAI unveiled a GPT-6 agent that writes and reviews code.</description>
<pubDate>Thu, 26 Nov 2026 11:00:00 +0000</pubDate></item>
</channel></rss>"""

HN_PICK_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Hacker News</title>
<item><title>OpenAI launches GPT-6 coding agent</title>
<link>https://example.com/hn/gpt6</link>
<description>Comments</description>
<pubDate>Thu, 26 Nov 2026 11:30:00 +0000</pubDate></item>
<item><title>Anthropic Claude update from last week</title>
<link>https://example.com/hn/claude-old</link>
<description>Claude gains a new feature.</description>
<pubDate>Thu, 20 Nov 2026 12:00:00 +0000</pubDate></item>
<item><title>Show HN: a new note-taking app</title>
<link>https://example.com/hn/notes</link>
<description>A minimal notes app.</description>
<pubDate>Thu, 26 Nov 2026 10:00:00 +0000</pubDate></item>
<item><title>He said it again</title>
<link>https://example.com/hn/said</link>
<description>Nothing about the topic here.</description>
<pubDate>Thu, 26 Nov 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""

TECHMEME_PICK_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Techmeme</title>
<item><title>OpenAI GPT-6 agent writes code, researchers say (The Verge)</title>
<link>https://example.com/tm/gpt6</link>
<description>OpenAI's new agent writes production code.</description>
<pubDate>Thu, 26 Nov 2026 11:45:00 +0000</pubDate></item>
<item><title>Local football final draws record crowd</title>
<link>https://example.com/tm/football</link>
<description>A derby match dominates the weekend.</description>
<pubDate>Thu, 26 Nov 2026 09:00:00 +0000</pubDate></item>
</channel></rss>"""

BING_PICK_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Bing News</title>
<item><title>Nvidia unveils new AI training chip</title>
<link>https://example.com/bing/nvidia-chip</link>
<description>Nvidia says the chip trains models twice as fast.</description>
<pubDate>Thu, 26 Nov 2026 11:15:00 +0000</pubDate></item>
</channel></rss>"""


def _picker_opener_for(news: bytes, hn: bytes, techmeme: bytes, bing: bytes):
    def opener(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "news.ycombinator.com" in url:
            return _FakeResponse(hn)
        if "techmeme.com" in url:
            return _FakeResponse(techmeme)
        if "bing.com" in url:
            return _FakeResponse(bing)
        return _FakeResponse(news)

    return opener


class TestTrendParsing(unittest.TestCase):
    def test_parse_news_rss_returns_real_items_only(self):
        items = parse_news_rss(NEWS_RSS)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "OpenAI launches new AI agent tool")
        self.assertEqual(items[0].source, "google_news")
        self.assertEqual(items[0].url, "https://example.com/news/openai-agent")

    def test_parse_news_rss_empty_feed(self):
        self.assertEqual(parse_news_rss(EMPTY_RSS), [])

    def test_parse_trends_rss_keeps_niche_relevant_only(self):
        items = parse_trends_rss(TRENDS_RSS, topic="OpenAI GPT models training")
        titles = [i.title for i in items]
        self.assertIn("GPT-5 leak", titles)
        self.assertNotIn("Local football final", titles)
        self.assertTrue(all(i.source == "google_trends_daily" for i in items))

    def test_parse_trends_rss_ai_needs_word_boundary(self):
        # "ai" as a substring must NOT match: "said" contains "ai" but
        # is not the AI niche. A headline with no whole-word AI keyword
        # and no topic overlap is dropped.
        rss = (
            b'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0">'
            b"<channel><title>T</title>"
            b"<item><title>He said it again</title>"
            b"<link>https://example.com/x</link>"
            b"<description>Nothing about the topic here.</description></item>"
            b"</channel></rss>"
        )
        self.assertEqual(parse_trends_rss(rss, topic="OpenAI GPT models training"), [])


class TestFetchTrends(unittest.TestCase):
    def test_fetch_trends_angles_come_from_real_items(self):
        report = fetch_trends("OpenAI GPT models training", opener=_opener_for(NEWS_RSS, TRENDS_RSS))
        self.assertTrue(report.has_trends)
        self.assertIn("google_news", report.sources)
        self.assertIn("google_trends_daily", report.sources)
        # Every angle must quote a real feed headline — nothing invented.
        self.assertTrue(any("OpenAI launches new AI agent tool" in a for a in report.angles))
        self.assertEqual(report.errors, [])

    def test_fetch_trends_never_raises_when_network_dead(self):
        report = fetch_trends("OpenAI GPT models training", opener=_failing_opener)
        self.assertEqual(report.angles, [])
        self.assertFalse(report.has_trends)
        self.assertEqual(report.sources, [])
        self.assertEqual(len(report.errors), 2)

    def test_fetch_trends_empty_feeds_yield_no_angles(self):
        report = fetch_trends("OpenAI GPT models training", opener=_opener_for(EMPTY_RSS, EMPTY_RSS))
        self.assertEqual(report.angles, [])
        self.assertEqual(report.items, [])
        self.assertTrue(report.errors)  # both sources report "no items"


class TestTrendNode(unittest.TestCase):
    def test_node_trends_attaches_angles_and_continues(self):
        graph = TubePilotGraph()
        state = PipelineState(
            topic="New AI tools this week",
            pillar="ai_everything",
            research=make_test_bundle(),
        )
        report = fetch_trends("New AI tools this week", opener=_opener_for(NEWS_RSS, TRENDS_RSS))
        with patch("orchestration.graph.fetch_trends", return_value=report):
            state = graph.node_trends(state)
        self.assertEqual(state.stage, "trends")
        self.assertEqual(state.status, "running")  # never failed by trends
        self.assertIsNotNone(state.trend_report)
        self.assertEqual(state.research.trend_angles, report.angles)
        self.assertEqual(state.research.trend_sources, report.sources)
        # Recorded in the yt_trends Sheet tab (fake Sheets).
        from db.sheets import get_trend_report

        saved = get_trend_report("New AI tools this week")
        self.assertIsNotNone(saved)
        self.assertEqual(saved["angles"], report.angles)

    def test_node_trends_never_fails_run_when_everything_breaks(self):
        graph = TubePilotGraph()
        state = PipelineState(
            topic="New AI tools this week",
            pillar="ai_everything",
            research=make_test_bundle(),
        )
        with (
            patch("orchestration.graph.fetch_trends", side_effect=RuntimeError("boom")),
            patch("orchestration.graph.save_trend_report", side_effect=RuntimeError("sheets down")),
        ):
            state = graph.node_trends(state)
        self.assertEqual(state.status, "running")
        self.assertIsNotNone(state.trend_report)
        self.assertEqual(state.trend_report.angles, [])


class TestFrontpageParsing(unittest.TestCase):
    def test_parse_frontpage_rss_keeps_ai_only(self):
        items = parse_frontpage_rss(HN_PICK_RSS, source="hacker_news")
        titles = [i.title for i in items]
        self.assertIn("OpenAI launches GPT-6 coding agent", titles)
        self.assertIn("Anthropic Claude update from last week", titles)
        self.assertNotIn("Show HN: a new note-taking app", titles)
        self.assertNotIn("He said it again", titles)  # "ai" in "said" must not match
        self.assertTrue(all(i.source == "hacker_news" for i in items))

    def test_parse_frontpage_rss_keeps_pubdate(self):
        items = parse_frontpage_rss(HN_PICK_RSS, source="hacker_news")
        fresh = [i for i in items if i.title.startswith("OpenAI launches")][0]
        self.assertEqual(fresh.published_at, calendar.timegm((2026, 11, 26, 11, 30, 0)))

    def test_parse_news_rss_source_param(self):
        items = parse_news_rss(BING_PICK_RSS, source="bing_news")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "bing_news")
        self.assertEqual(items[0].title, "Nvidia unveils new AI training chip")


class TestTopicFromHeadline(unittest.TestCase):
    def test_strips_publisher_suffix(self):
        self.assertEqual(
            topic_from_headline("OpenAI launches GPT-6 agent for coding - The Verge"),
            "OpenAI launches GPT-6 agent for coding",
        )

    def test_leaves_lowercase_tail_alone(self):
        self.assertEqual(
            topic_from_headline("AI - the future is now"),
            "AI - the future is now",
        )

    def test_leaves_short_headline_alone(self):
        self.assertEqual(topic_from_headline("AI boom - Reuters"), "AI boom - Reuters")


class TestPickDailyTrends(unittest.TestCase):
    def _pick(self):
        opener = _picker_opener_for(GN_PICK_RSS, HN_PICK_RSS, TECHMEME_PICK_RSS, BING_PICK_RSS)
        return pick_daily_trends(opener=opener, now=_PICK_NOW)

    def test_clusters_cross_source_story_first(self):
        pick = self._pick()
        self.assertTrue(pick.has_pick)
        self.assertEqual(len(pick.topics), 2)
        best = pick.best_topic
        self.assertEqual(best.topic, "OpenAI launches GPT-6 agent for coding")
        # Best item 2.0 + 0.5 per extra source (2) + 0.1 per extra item (2).
        self.assertAlmostEqual(best.score, 3.2)
        self.assertEqual(best.sources, ["google_news", "hacker_news", "techmeme"])
        self.assertEqual(len(best.items), 3)
        self.assertEqual(pick.sources, ["google_news", "hacker_news", "techmeme", "bing_news"])
        self.assertEqual(pick.errors, [])

    def test_single_source_fresh_story_is_second_pick(self):
        pick = self._pick()
        second = pick.topics[1]
        self.assertEqual(second.topic, "Nvidia unveils new AI training chip")
        self.assertAlmostEqual(second.score, 2.0)
        self.assertEqual(second.sources, ["bing_news"])

    def test_stale_story_is_not_picked(self):
        pick = self._pick()
        all_titles = [i.title for t in pick.topics for i in t.items]
        self.assertNotIn("Anthropic Claude update from last week", all_titles)

    def test_empty_feeds_yield_no_pick(self):
        opener = _picker_opener_for(EMPTY_RSS, EMPTY_RSS, EMPTY_RSS, EMPTY_RSS)
        pick = pick_daily_trends(opener=opener, now=_PICK_NOW)
        self.assertFalse(pick.has_pick)
        self.assertIsNone(pick.best_topic)
        self.assertEqual(len(pick.errors), 4)

    def test_picker_never_raises_when_network_dead(self):
        pick = pick_daily_trends(opener=_failing_opener, now=_PICK_NOW)
        self.assertFalse(pick.has_pick)
        self.assertEqual(len(pick.errors), 4)

    def test_trend_report_for_pick(self):
        pick = self._pick()
        report = trend_report_for_pick(pick)
        self.assertEqual(report.topic, "OpenAI launches GPT-6 agent for coding")
        self.assertEqual(report.angles, pick.best_topic.angles)
        self.assertEqual(report.sources, pick.best_topic.sources)
        self.assertEqual(report.pillar, "ai_everything")


class TestDailyRunTrendingPreference(unittest.TestCase):
    """_resolve_topic prefers a strong trending pick over the queue."""

    def _args(self, **overrides):
        import argparse

        base = {
            "topic": None,
            "pillar": "ai_everything",
            "scheduled": True,
            "allow_default_topic": False,
        }
        base.update(overrides)
        return argparse.Namespace(**base)

    def _strong_pick(self, topic="OpenAI launches GPT-6 agent for coding"):
        return DailyTrendPick(
            topics=[
                TrendingTopic(
                    topic=topic,
                    score=3.2,
                    sources=["google_news", "hacker_news", "techmeme"],
                    items=[],
                    angles=[f"Trending now: {topic}"],
                )
            ],
            sources=["google_news", "hacker_news", "techmeme"],
            errors=[],
        )

    def test_trending_preferred_over_stale_queue(self):
        from scripts.daily_run import _resolve_topic

        trending = "OpenAI launches GPT-6 agent for coding"
        with (
            patch("scripts.daily_run.pick_daily_trends", return_value=self._strong_pick(trending)),
            patch("scripts.daily_run.list_topics_queue", return_value=[{"topic": "Old queued AI tools topic"}]),
            patch("scripts.daily_run.enqueue_topic", return_value="top_abc") as enqueue,
            patch("scripts.daily_run.dequeue_next_topic", return_value={"topic": trending, "pillar": "ai_everything"}),
            patch("scripts.daily_run.save_trend_report", return_value="hash") as save,
        ):
            self.assertEqual(_resolve_topic(self._args()), trending)
        enqueue.assert_called_once_with(trending, pillar="ai_everything", priority=2)
        save.assert_called_once()

    def test_trending_rejected_by_filter_falls_back_to_queue(self):
        from scripts.daily_run import _resolve_topic

        queued = "Old queued AI tools topic"
        with (
            patch("scripts.daily_run.pick_daily_trends", return_value=self._strong_pick("best poses for sex")),
            patch("scripts.daily_run.list_topics_queue", return_value=[]),
            patch("scripts.daily_run.enqueue_topic") as enqueue,
            patch("scripts.daily_run.dequeue_next_topic", return_value={"topic": queued, "pillar": "ai_everything"}),
            patch("scripts.daily_run.save_trend_report") as save,
        ):
            self.assertEqual(_resolve_topic(self._args()), queued)
        enqueue.assert_not_called()
        save.assert_not_called()

    def test_trending_already_queued_is_not_duplicated(self):
        from scripts.daily_run import _resolve_topic

        trending = "OpenAI launches GPT-6 agent for coding"
        with (
            patch("scripts.daily_run.pick_daily_trends", return_value=self._strong_pick(trending)),
            patch("scripts.daily_run.list_topics_queue", return_value=[{"topic": trending}]),
            patch("scripts.daily_run.enqueue_topic") as enqueue,
            patch("scripts.daily_run.dequeue_next_topic", return_value={"topic": trending, "pillar": "ai_everything"}),
        ):
            self.assertEqual(_resolve_topic(self._args()), trending)
        enqueue.assert_not_called()

    def test_no_strong_trend_falls_back_to_queue(self):
        from scripts.daily_run import _resolve_topic

        queued = "Old queued AI tools topic"
        with (
            patch("scripts.daily_run.pick_daily_trends", return_value=DailyTrendPick(topics=[], sources=["google_news"], errors=[])),
            patch("scripts.daily_run.enqueue_topic") as enqueue,
            patch("scripts.daily_run.dequeue_next_topic", return_value={"topic": queued, "pillar": "ai_everything"}),
        ):
            self.assertEqual(_resolve_topic(self._args()), queued)
        enqueue.assert_not_called()

    def test_no_trending_flag_skips_picker(self):
        from scripts.daily_run import _resolve_topic

        queued = "Old queued AI tools topic"
        with (
            patch("scripts.daily_run.pick_daily_trends", side_effect=AssertionError("picker must not run")),
            patch("scripts.daily_run.dequeue_next_topic", return_value={"topic": queued, "pillar": "ai_everything"}),
        ):
            self.assertEqual(_resolve_topic(self._args(no_trending=True)), queued)


if __name__ == "__main__":
    unittest.main()
