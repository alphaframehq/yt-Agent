"""Tests for the run #14 research fix (26 Nov 2026).

Diagnosis: TinyFish moved its Search API from
``https://api.tinyfish.io/v1/search`` (POST, Bearer) to
``https://api.search.tinyfish.ai`` (GET, ``X-API-Key``). The old host
is NXDOMAIN in public DNS (8.8.8.8 / 1.1.1.1 return no records), so
the GitHub Actions runner died with gaierror EAI_NONAME at the
research node and the whole daily run failed in ~90s.

Fix under test:
1. ``search_fintech_context()`` calls the new TinySearch host with
   the new auth + response shape.
2. When TinySearch is unreachable / errors / returns zero articles
   (or the key is missing), research falls back to real keyless RSS
   articles (``tools/trends.py``) — never fabricated, never a hard
   fail of the run.
3. Only when BOTH sources yield nothing does it raise.
4. ``record_pipeline_run()`` writes ``extra_json`` as a JSON string
   (``yt_pipeline_runs`` was missing from ``JSON_COLUMNS``, so the
   Sheets API rejected every telemetry row with "Invalid values").

No network, no real keys, no real Sheets: ``httpx.Client`` is faked,
``fetch_trends`` is patched, Sheets uses ``tests/_sheets_fake.py``.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Add yt-agent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from tests._sheets_fake import install_fake_sheets

_FAKE_SHEETS = None
_SHEETS_PATCHER = None


def setUpModule():
    global _FAKE_SHEETS, _SHEETS_PATCHER
    _FAKE_SHEETS, _SHEETS_PATCHER = install_fake_sheets()


def tearDownModule():
    if _SHEETS_PATCHER is not None:
        _SHEETS_PATCHER.stop()

import tools.tinyfish as tinyfish
from tools.trends import TrendItem, TrendReport

TINYSEARCH_URL = "https://api.search.tinyfish.ai"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _FakeClient:
    """Stand-in for httpx.Client. ``behavior`` is set per test:
    ``(url, headers, params) -> _FakeResponse`` or raises."""

    behavior = None
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, params=None):
        _FakeClient.calls.append(
            {"url": url, "headers": headers, "params": params}
        )
        return _FakeClient.behavior(url, headers, params)


def _rss_report():
    """A TrendReport with two real-looking RSS items (run #14 topic)."""
    return TrendReport(
        topic="LLM Training Kaise Hoti Hai?",
        pillar="ai_everything",
        angles=[],
        items=[
            TrendItem(
                title="How LLM Pretraining Actually Works",
                source="google_news",
                url="https://example.com/news/llm-pretraining",
                snippet="Pretraining on trillions of tokens, then RLHF.",
                published_at=1764000000,
            ),
            TrendItem(
                title="RLHF Explained: Why Human Feedback Matters",
                source="hacker_news",
                url="https://example.com/hn/rlhf-explained",
                snippet="Reward models rank outputs before fine-tuning.",
                published_at=1763900000,
            ),
        ],
        sources=["google_news", "hacker_news"],
        errors=[],
    )


def _tinysearch_payload(results):
    return {"query": "LLM Training", "results": results, "total_results": len(results)}


# ---------------------------------------------------------------------------
# TinySearch primary path
# ---------------------------------------------------------------------------

class TestTinySearchNewHost(unittest.TestCase):
    """The new host, auth, and response shape (run #14 root cause)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_cache = tinyfish.CACHE_DIR
        tinyfish.CACHE_DIR = Path(self._tmp.name)
        _FakeClient.calls = []
        _FakeClient.behavior = None
        self._client_patcher = patch.object(httpx, "Client", _FakeClient)
        self._client_patcher.start()
        self._key_patcher = patch.object(
            tinyfish, "get_secret", return_value="test-key"
        )
        self._key_patcher.start()

    def tearDown(self):
        self._client_patcher.stop()
        self._key_patcher.stop()
        tinyfish.CACHE_DIR = self._orig_cache
        self._tmp.cleanup()

    def test_gets_new_host_with_api_key_header(self):
        """GET api.search.tinyfish.ai, X-API-Key (not Bearer), news first."""
        def behavior(url, headers, params):
            self.assertEqual(url, TINYSEARCH_URL)
            self.assertEqual(headers, {"X-API-Key": "test-key"})
            self.assertEqual(params["domain_type"], "news")
            self.assertEqual(params["q"], "LLM Training Kaise Hoti Hai?")
            return _FakeResponse(200, _tinysearch_payload([
                {
                    "position": 1,
                    "site_name": "example.com",
                    "publisher": "Example AI News",
                    "title": "LLM Training Breakthrough",
                    "snippet": "New pretraining recipe cuts cost.",
                    "url": "https://example.com/llm-breakthrough",
                    "date": "2026-11-26T10:00:00Z",
                }
            ]))

        _FakeClient.behavior = behavior
        articles = tinyfish.search_fintech_context("LLM Training Kaise Hoti Hai?")
        self.assertEqual(len(_FakeClient.calls), 1)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["title"], "LLM Training Breakthrough")
        self.assertEqual(articles[0]["source"], "Example AI News")
        self.assertEqual(articles[0]["published_at"], "2026-11-26T10:00:00Z")
        self.assertEqual(articles[0]["url"], "https://example.com/llm-breakthrough")

    def test_news_400_falls_through_to_web(self):
        """Run #15: TinySearch HTTP 400 on news must NOT skip web.

        The run #15 topic (long Hindi-mixed question) got HTTP 400
        for domain_type=news and the old code raised out of the loop
        before web was ever tried. Now news errors fall through.
        """
        calls = []

        def behavior(url, headers, params):
            calls.append(params["domain_type"])
            if params["domain_type"] == "news":
                return _FakeResponse(400, {"error": "bad request"})
            return _FakeResponse(200, _tinysearch_payload([
                {
                    "position": 1,
                    "site_name": "example.org",
                    "title": "LLM Training Guide",
                    "snippet": "A practical guide.",
                    "url": "https://example.org/llm-guide",
                }
            ]))

        _FakeClient.behavior = behavior
        articles = tinyfish.search_fintech_context("LLM Training 400 Topic")
        self.assertEqual(calls, ["news", "web"])
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["title"], "LLM Training Guide")

    def test_news_error_then_web_empty_falls_back_to_rss(self):
        """News 400 + web zero results -> RSS bundle, not a dead run."""
        calls = []

        def behavior(url, headers, params):
            calls.append(params["domain_type"])
            if params["domain_type"] == "news":
                return _FakeResponse(400, {"error": "bad request"})
            return _FakeResponse(200, _tinysearch_payload([]))

        _FakeClient.behavior = behavior
        with patch(
            "tools.trends.fetch_trends", return_value=_rss_report()
        ):
            articles = tinyfish.search_fintech_context("LLM Training 400 Zero")
        # One retry of the whole query (tenacity, 2 attempts): the
        # run is saved by RSS, not by the retry.
        self.assertEqual(calls, ["news", "web", "news", "web"])
        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0]["source"], "Google News")

    def test_both_domain_types_error_and_rss_empty_raises(self):
        """News 400 + web 400 + empty RSS -> loud failure, never invented."""
        _FakeClient.behavior = lambda u, h, p: _FakeResponse(
            400, {"error": "bad request"}
        )
        with patch(
            "tools.trends.fetch_trends",
            return_value=TrendReport(
                topic="LLM Training Dead 400",
                pillar="ai_everything",
                angles=[],
                items=[],
                sources=[],
                errors=["google_news: feed returned no items"],
            ),
        ):
            with self.assertRaises(RuntimeError):
                tinyfish.search_fintech_context("LLM Training Dead 400")


# ---------------------------------------------------------------------------
# RSS fallback
# ---------------------------------------------------------------------------

class TestTinySearchRssFallback(unittest.TestCase):
    """A dead TinySearch must fall back to real RSS articles, not fail."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_cache = tinyfish.CACHE_DIR
        tinyfish.CACHE_DIR = Path(self._tmp.name)
        _FakeClient.calls = []
        _FakeClient.behavior = None
        self._client_patcher = patch.object(httpx, "Client", _FakeClient)
        self._client_patcher.start()
        self._key_patcher = patch.object(
            tinyfish, "get_secret", return_value="test-key"
        )
        self._key_patcher.start()
        self._rss_patcher = patch(
            "tools.trends.fetch_trends", return_value=_rss_report()
        )
        self._rss_patcher.start()

    def tearDown(self):
        self._rss_patcher.stop()
        self._client_patcher.stop()
        self._key_patcher.stop()
        tinyfish.CACHE_DIR = self._orig_cache
        self._tmp.cleanup()

    def test_dns_failure_falls_back_to_rss(self):
        """gaierror-style ConnectError (run #14's exact failure) -> RSS bundle."""
        def behavior(url, headers, params):
            raise httpx.ConnectError(
                "[Errno -5] No address associated with hostname",
                request=httpx.Request("GET", url),
            )

        _FakeClient.behavior = behavior
        articles = tinyfish.search_fintech_context("LLM Training DNS Topic")
        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0]["title"], "How LLM Pretraining Actually Works")
        self.assertEqual(articles[0]["source"], "Google News")
        self.assertEqual(articles[0]["url"], "https://example.com/news/llm-pretraining")
        self.assertEqual(articles[1]["source"], "Hacker News")
        # RSS fallback results are NOT cached (a recovered TinySearch
        # must be picked up on the next run).
        self.assertEqual(list(Path(self._tmp.name).glob("*.json")), [])

    def test_zero_results_falls_back_to_rss(self):
        """TinySearch 200 with zero results (news + web) -> RSS bundle."""
        _FakeClient.behavior = lambda u, h, p: _FakeResponse(
            200, _tinysearch_payload([])
        )
        articles = tinyfish.search_fintech_context("LLM Training Zero Topic")
        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0]["title"], "How LLM Pretraining Actually Works")

    def test_missing_key_falls_back_to_rss(self):
        """No TINYFISH_API_KEY -> RSS fallback, no HTTP attempt at all."""
        self._key_patcher.stop()
        self._key_patcher = patch.object(tinyfish, "get_secret", return_value=None)
        self._key_patcher.start()
        articles = tinyfish.search_fintech_context("LLM Training No Key Topic")
        self.assertEqual(_FakeClient.calls, [])
        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0]["source"], "Google News")

    def test_both_sources_dead_raises(self):
        """TinySearch dead AND RSS empty -> loud failure, never invented."""
        _FakeClient.behavior = lambda u, h, p: _FakeResponse(
            200, _tinysearch_payload([])
        )
        self._rss_patcher.stop()
        self._rss_patcher = patch(
            "tools.trends.fetch_trends",
            return_value=TrendReport(
                topic="LLM Training Dead Topic",
                pillar="ai_everything",
                angles=[],
                items=[],
                sources=[],
                errors=["google_news: boom"],
            ),
        )
        self._rss_patcher.start()
        with self.assertRaises(RuntimeError):
            tinyfish.search_fintech_context("LLM Training Dead Topic")


# ---------------------------------------------------------------------------
# Sheets telemetry fix (run #14 "Invalid values" warning)
# ---------------------------------------------------------------------------

class TestRecordPipelineRunSheets(unittest.TestCase):
    """extra_json must be a JSON string — the Sheets API rejects dicts."""

    def test_failed_run_records_without_invalid_values(self):
        from db.sheets import record_pipeline_run

        run_id = record_pipeline_run({
            "kind": "daily_run",
            "topic": "LLM Training Kaise Hoti Hai? Pretraining Se RLHF Tak — Simple Hindi Me",
            "pillar": "ai_everything",
            "long_status": "failed",
            "long_video_id": None,
            "shorts_overall": "skipped",
            "shorts_uploaded": 0,
            "shorts_video_ids": [],
            "cross_linked": False,
        })
        rows = _FAKE_SHEETS.store["yt_pipeline_runs"]
        self.assertEqual(len(rows), 2)  # header + 1 data row
        headers = rows[0]
        data = rows[1]
        self.assertEqual(headers, ["run_id", "started_at", "ttl", "extra_json"])
        self.assertEqual(data[0], run_id)
        # The extra_json cell must be a plain string (what the real
        # Sheets API accepts) — before the fix it was a raw dict.
        extra_cell = data[3]
        self.assertIsInstance(extra_cell, str)
        extra = json.loads(extra_cell)
        self.assertEqual(extra["long_status"], "failed")
        self.assertIsNone(extra["long_video_id"])
        self.assertEqual(extra["shorts_video_ids"], [])
        self.assertFalse(extra["cross_linked"])
        # No cell may hold a dict/list (the real API rejects those
        # with "Invalid values").
        for cell in data:
            self.assertNotIsInstance(cell, (dict, list))


# ---------------------------------------------------------------------------
# End-to-end: AI research bundle from the RSS fallback
# ---------------------------------------------------------------------------

class TestAiResearchRssFallback(unittest.TestCase):
    """ResearchAgent._run_ai_topic must return a real bundle when
    TinySearch is dead (the run #14 failure path)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_cache = tinyfish.CACHE_DIR
        tinyfish.CACHE_DIR = Path(self._tmp.name)
        _FakeClient.calls = []

        def _dns_dead(url, headers, params):
            raise httpx.ConnectError(
                "[Errno -5] No address associated with hostname",
                request=httpx.Request("GET", url),
            )

        _FakeClient.behavior = _dns_dead
        self._client_patcher = patch.object(httpx, "Client", _FakeClient)
        self._client_patcher.start()
        self._key_patcher = patch.object(
            tinyfish, "get_secret", return_value="test-key"
        )
        self._key_patcher.start()
        self._rss_patcher = patch(
            "tools.trends.fetch_trends", return_value=_rss_report()
        )
        self._rss_patcher.start()
        self._yt_patcher = patch(
            "agents.research.research_topic",
            return_value={
                "top_titles": ["LLM Training Explained in Hindi"],
                "common_tags": ["llm", "training"],
                "gaps": ["No Hindi explainer on RLHF yet"],
            },
        )
        self._yt_patcher.start()

    def tearDown(self):
        self._yt_patcher.stop()
        self._rss_patcher.stop()
        self._client_patcher.stop()
        self._key_patcher.stop()
        tinyfish.CACHE_DIR = self._orig_cache
        self._tmp.cleanup()

    def test_ai_topic_bundle_built_from_rss(self):
        from agents.research import ResearchAgent, ResearchInput

        bundle = ResearchAgent().run(ResearchInput(
            topic="LLM Training Kaise Hoti Hai? Pretraining Se RLHF Tak — Simple Hindi Me",
            pillar="ai_everything",
        ))
        self.assertEqual(len(bundle.sources), 2)
        self.assertIn("How LLM Pretraining Actually Works", bundle.technical_context)
        self.assertIn("RLHF Explained", bundle.technical_context)
        self.assertEqual(bundle.sources[0].source, "Google News")
        self.assertEqual(
            bundle.sources[0].url, "https://example.com/news/llm-pretraining"
        )
        self.assertGreater(bundle.research_confidence, 0)


if __name__ == "__main__":
    unittest.main()
