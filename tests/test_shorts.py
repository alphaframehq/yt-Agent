"""Unit tests for the Shorts path: #Shorts SEO + separate upload
(tools/youtube_api.py, agents/uploader.py), per-Short isolation
(agents/shorts.py), and separate Sheet tracking (db/sheets.py
yt_shorts / yt_trends tabs).

No network, no YouTube, no real Sheets: uploads are mocked, the
ffprobe duration probe is mocked, and Sheets uses tests/_sheets_fake.
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


from agents.shorts import render_and_upload_shorts, summarize_shorts, upload_shorts
from agents.uploader import UploadResult, UploaderAgent
from db.sheets import (
    get_short_record,
    get_trend_report,
    list_recent_shorts,
    save_short_record,
    save_trend_report,
    update_short_status,
)
from models.script import SceneInfo, ScriptMeta, SEOData, ShortsScript
from tools.trends import TrendReport
from tools.youtube_api import shorts_seo, upload_short

LONG_DESC = (
    "DISCLAIMER: For educational and engineering analysis only. "
    + " ".join(["This short explains how TestCo operates its payment network."] * 60)
)


def make_test_short(title="TestCo in 60 seconds"):
    """A valid ShortsScript (<=3 scenes, <=60s, 9:16, disclaimer SEO)."""
    scenes = [
        SceneInfo(
            id=1, section="hook",
            narration="TestCo reported revenue of $1.2 billion in 2024.",
            visual_prompt="TestCo hook diagram", visual_type="chart",
            chart_type="revenue_trend", duration_sec=20,
            citations=["revenue_2024"],
        ),
        SceneInfo(
            id=2, section="cta",
            narration="Follow for more breakdowns of payment networks.",
            visual_prompt="TestCo cta diagram", visual_type="diagram",
            chart_type=None, duration_sec=15, citations=[],
        ),
    ]
    return ShortsScript(
        pillar="finance_to_technical",
        scenes=scenes,
        seo=SEOData(
            title=title,
            description=LONG_DESC,
            tags=[f"testco{i}" for i in range(16)],
        ),
        meta=ScriptMeta(word_count=20, estimated_duration_sec=35),
    )


def _upload_result(yt_id="short_yt_1"):
    return UploadResult(
        video_id=f"db_{yt_id}", yt_video_id=yt_id,
        yt_url=f"https://youtu.be/{yt_id}", title="TestCo in 60 seconds",
        format="short", sheets_saved=True,
    )


class TestShortsSeo(unittest.TestCase):
    def test_shorts_seo_adds_shorts_tag_once(self):
        seo = {"title": "TestCo in 60 seconds", "description": LONG_DESC,
               "tags": ["testco", "fintech"]}
        out = shorts_seo(seo)
        self.assertIn("#Shorts", out["title"])
        self.assertTrue(out["description"].startswith("#Shorts"))
        self.assertIn("shorts", [t.lower() for t in out["tags"]])
        # Idempotent: running it again does not duplicate #Shorts.
        out2 = shorts_seo(out)
        self.assertEqual(out2["title"].count("#Shorts"), 1)
        self.assertEqual(out2["description"].count("#Shorts"), 1)

    def test_shorts_seo_truncates_long_title_to_100_chars(self):
        seo = {"title": "X" * 120, "description": LONG_DESC, "tags": []}
        out = shorts_seo(seo)
        self.assertLessEqual(len(out["title"]), 100)
        self.assertTrue(out["title"].endswith("#Shorts"))

    def test_shorts_seo_requires_title(self):
        with self.assertRaises(ValueError):
            shorts_seo({"title": "", "description": LONG_DESC, "tags": []})

    def test_upload_short_passes_shorts_seo_to_upload_private(self):
        seen = {}

        def fake_upload_private(video_path, thumb_path, seo_data):
            seen.update(seo_data)
            return "yt_short_123"

        with patch("tools.youtube_api.upload_private", side_effect=fake_upload_private):
            vid = upload_short("/tmp/fake.mp4", None, {
                "title": "TestCo in 60 seconds", "description": LONG_DESC,
                "tags": ["testco"],
            })
        self.assertEqual(vid, "yt_short_123")
        self.assertIn("#Shorts", seen["title"])
        self.assertTrue(seen["description"].startswith("#Shorts"))


class TestShortSheets(unittest.TestCase):
    def test_save_and_get_short_record(self):
        short_id = save_short_record({
            "parent_topic": "How TestCo Makes Money",
            "topic": "TestCo in 60 seconds",
            "yt_video_id": "yt_short_1",
            "yt_url": "https://youtu.be/yt_short_1",
            "title": "TestCo in 60 seconds",
            "format": "short",
        })
        rec = get_short_record(short_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["yt_video_id"], "yt_short_1")
        self.assertEqual(rec["parent_topic"], "How TestCo Makes Money")
        self.assertEqual(rec["status"], "pending_approval")

    def test_update_short_status(self):
        short_id = save_short_record({
            "parent_topic": "How TestCo Makes Money",
            "topic": "TestCo short",
            "yt_video_id": "yt_short_2",
            "yt_url": "https://youtu.be/yt_short_2",
            "title": "TestCo short",
        })
        self.assertTrue(update_short_status(short_id, "public", published_at=1234567890))
        self.assertEqual(get_short_record(short_id)["status"], "public")
        self.assertTrue(any(r["short_id"] == short_id for r in list_recent_shorts()))

    def test_save_and_get_trend_report(self):
        report = TrendReport(
            topic="How TestCo Makes Money",
            angles=["Trending now: X — what it means for How TestCo Makes Money"],
            sources=["google_news"],
        )
        save_trend_report("How TestCo Makes Money", report)
        saved = get_trend_report("How TestCo Makes Money")
        self.assertIsNotNone(saved)
        self.assertEqual(saved["angles"], report.angles)
        self.assertEqual(saved["sources"], ["google_news"])


class TestShortUploader(unittest.TestCase):
    def _patched_upload(self, probe_sec=30.0, sheets_fail=False):
        """Patch the Short uploader's external calls.

        Returns (patchers, mocks): stop the patchers in tearDown, use
        the mocks for assertions (same order: probe, upload_short,
        verify, purge, [save_short_record]).
        """
        patchers = []
        mocks = []
        for target, kw in (
            ("agents.uploader._probe_duration_sec", {"return_value": probe_sec}),
            ("agents.uploader.upload_short", {"return_value": "yt_short_9"}),
            ("agents.uploader.verify_video_exists", {"return_value": True}),
            ("agents.uploader.purge_local_media", {}),
        ):
            p = patch(target, **kw)
            patchers.append(p)
            mocks.append(p.start())
        if sheets_fail:
            p = patch("agents.uploader.save_short_record", side_effect=RuntimeError("sheets down"))
            patchers.append(p)
            mocks.append(p.start())
        return patchers, mocks

    def test_upload_short_registers_in_yt_shorts_not_yt_videos(self):
        tmp = Path("/tmp/test_short_upload.mp4")
        tmp.write_bytes(b"x" * 20_000)
        try:
            patchers, _mocks = self._patched_upload()
            try:
                result = UploaderAgent().upload_short_and_register(
                    video_path=str(tmp), thumb_path=None,
                    short_script=make_test_short(),
                    parent_topic="How TestCo Makes Money", short_index=0,
                )
            finally:
                for p in patchers:
                    p.stop()
            self.assertEqual(result.format, "short")
            self.assertEqual(result.yt_video_id, "yt_short_9")
            self.assertTrue(result.sheets_saved)
            rec = get_short_record(result.video_id)
            self.assertIsNotNone(rec)
            self.assertEqual(rec["parent_topic"], "How TestCo Makes Money")
            from db.sheets import get_video_record

            self.assertIsNone(get_video_record(result.video_id))
        finally:
            if tmp.exists():
                tmp.unlink()

    def test_upload_short_rejects_over_60s_render(self):
        tmp = Path("/tmp/test_short_upload.mp4")
        tmp.write_bytes(b"x" * 20_000)
        try:
            patchers, _mocks = self._patched_upload(probe_sec=61.0)
            try:
                with self.assertRaises(ValueError):
                    UploaderAgent().upload_short_and_register(
                        video_path=str(tmp), thumb_path=None,
                        short_script=make_test_short(),
                        parent_topic="How TestCo Makes Money", short_index=0,
                    )
            finally:
                for p in patchers:
                    p.stop()
        finally:
            if tmp.exists():
                tmp.unlink()

    def test_upload_short_sheets_failure_keeps_video_and_file(self):
        tmp = Path("/tmp/test_short_upload.mp4")
        tmp.write_bytes(b"x" * 20_000)
        try:
            patchers, mocks = self._patched_upload(sheets_fail=True)
            purge_mock = None
            try:
                # purge mock is the 4th mock (index 3)
                purge_mock = mocks[3]
                result = UploaderAgent().upload_short_and_register(
                    video_path=str(tmp), thumb_path=None,
                    short_script=make_test_short(),
                    parent_topic="How TestCo Makes Money", short_index=0,
                )
            finally:
                for p in patchers:
                    p.stop()
            self.assertEqual(result.yt_video_id, "yt_short_9")
            self.assertFalse(result.sheets_saved)  # upload stands, Sheets missed
            self.assertEqual(result.purged_local_bytes, 0)
            purge_mock.assert_not_called()
            self.assertTrue(tmp.exists())  # file kept for manual re-register
        finally:
            if tmp.exists():
                tmp.unlink()


class TestShortsIsolation(unittest.TestCase):
    def test_one_short_failure_does_not_kill_the_others(self):
        tmp1 = Path("/tmp/test_short_a.mp4")
        tmp2 = Path("/tmp/test_short_b.mp4")
        tmp1.write_bytes(b"x" * 20_000)
        tmp2.write_bytes(b"x" * 20_000)
        try:
            calls = {"n": 0}

            def fake_run_upload_short(**kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("YouTube upload failed (fixture)")
                return _upload_result("short_yt_2")

            with patch("agents.shorts.run_upload_short", side_effect=fake_run_upload_short):
                results = upload_shorts(
                    [(str(tmp1), make_test_short("Short A")),
                     (str(tmp2), make_test_short("Short B"))],
                    parent_topic="How TestCo Makes Money",
                )
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0]["status"], "failed")
            self.assertEqual(results[1]["status"], "uploaded")
            self.assertEqual(results[1]["yt_video_id"], "short_yt_2")
            summary = summarize_shorts(results)
            self.assertEqual(summary["overall"], "partial")
            self.assertEqual(summary["uploaded"], 1)
        finally:
            for t in (tmp1, tmp2):
                if t.exists():
                    t.unlink()

    def test_render_and_upload_shorts_uses_rendered_pairs(self):
        tmp = Path("/tmp/test_short_c.mp4")
        tmp.write_bytes(b"x" * 20_000)
        try:
            from tests._fixtures import make_test_bundle

            with patch("agents.shorts.run_upload_short", return_value=_upload_result("short_yt_3")):
                results = render_and_upload_shorts(
                    make_test_bundle(),
                    parent_topic="How TestCo Makes Money",
                    count=1,
                    rendered=[(str(tmp), make_test_short())],
                )
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["status"], "uploaded")
        finally:
            if tmp.exists():
                tmp.unlink()

    def test_missing_render_file_is_skipped_not_failed(self):
        results = upload_shorts(
            [("/tmp/does-not-exist-short.mp4", make_test_short())],
            parent_topic="How TestCo Makes Money",
        )
        self.assertEqual(results[0]["status"], "skipped")


class TestDailyRunTopicResolution(unittest.TestCase):
    def test_scheduled_empty_queue_resolves_to_none(self):
        from scripts.daily_run import _resolve_topic
        from tools.trends import DailyTrendPick
        import argparse

        args = argparse.Namespace(topic=None, pillar="finance_to_technical",
                                  scheduled=True, allow_default_topic=False)
        # No trend today: the picker must not hit the network in this
        # test (module contract: no network) — an empty pick falls back
        # to the queue, which is empty, so a scheduled run resolves None.
        with patch("scripts.daily_run.pick_daily_trends",
                   return_value=DailyTrendPick()):
            self.assertIsNone(_resolve_topic(args))

    def test_explicit_topic_passes_through(self):
        from scripts.daily_run import _resolve_topic
        import argparse

        args = argparse.Namespace(topic="How TestCo Makes Money",
                                  pillar="finance_to_technical",
                                  scheduled=True, allow_default_topic=False)
        self.assertEqual(_resolve_topic(args), "How TestCo Makes Money")


if __name__ == "__main__":
    unittest.main()
