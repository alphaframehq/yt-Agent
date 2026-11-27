"""Internal linking tests (26 Nov 2026): description templates,
references from real bundle sources, related-videos Sheet reads,
post-Shorts cross-linking, and the videos.update helper.

No live API calls: YouTube/Sheets are mocked or faked
(tests/_sheets_fake.py). The description builder is pure and tested
without any service.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Add yt-agent to path
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


BASE_DESCRIPTION = (
    "DISCLAIMER: For educational purposes only. "
    + " ".join(["This breakdown explains the new AI tool."] * 60)
)


def _make_ai_bundle():
    """Small ai_everything ResearchBundle with real retrieved sources."""
    from models.research import (
        EntityInfo,
        FinancialData,
        MarginData,
        MarketData,
        ResearchBundle,
        SourceInfo,
        YTInsights,
    )

    return ResearchBundle(
        topic="Tool X launches",
        pillar="ai_everything",
        entities=[EntityInfo(name="Tool X", type="Topic", facts=[])],
        financials=FinancialData(
            revenue_by_year={},
            segments={},
            margins=MarginData(gross=0.0, operating=0.0, net=0.0),
            growth_rate=0.0,
            unit_economics={},
        ),
        technical_context="Tool X is a new AI tool.",
        competitors=[],
        market_data=MarketData(tam_usd=0.0, penetration_pct=0.0),
        sources=[
            SourceInfo(
                claim_id="context_0", claim="Tool X launches",
                source="TechCrunch", url="https://example.com/tc",
                snippet="Tool X launches today.", confidence=0.9,
            ),
            SourceInfo(
                claim_id="context_1", claim="Tool X pricing",
                source="The Verge", url="https://example.com/v",
                snippet="Tool X costs $20.", confidence=0.9,
            ),
        ],
        yt_insights=YTInsights(
            top_titles=["Tool X first look"], common_tags=[], gaps=[],
        ),
        research_confidence=0.8,
        trend_angles=["Tool X is trending"],
        trend_sources=["google_news", "google_trends_daily"],
    )


def _make_short_script():
    """A valid ShortsScript with a disclaimer-led 300+ word description."""
    from models.script import ChapterInfo, SceneInfo, ScriptMeta, SEOData, ShortsScript

    description = BASE_DESCRIPTION
    scenes = [
        SceneInfo(
            id=1, section="hook",
            narration="Tool X launched today and it changes how builders work.",
            visual_prompt="Tool X launch visual",
            visual_type="ai_image", duration_sec=25, citations=["context_0"],
        ),
        SceneInfo(
            id=2, section="cta",
            narration="Follow for the full breakdown in the long video.",
            visual_prompt="Subscribe visual",
            visual_type="ai_image", duration_sec=10, citations=[],
        ),
    ]
    return ShortsScript(
        pillar="ai_everything",
        scenes=scenes,
        seo=SEOData(
            title="Tool X Just Launched",
            description=description,
            tags=[f"toolx{i}" for i in range(16)],
            chapters=[ChapterInfo(time="00:00", title="Intro")],
        ),
        meta=ScriptMeta(word_count=100, estimated_duration_sec=35),
    )


class TestBuildVideoDescription(unittest.TestCase):
    def test_base_only_is_unchanged(self):
        from agents.uploader import build_video_description

        self.assertEqual(build_video_description(BASE_DESCRIPTION), BASE_DESCRIPTION)

    def test_watch_next_and_sources_appended_in_order(self):
        from agents.uploader import build_video_description

        out = build_video_description(
            BASE_DESCRIPTION,
            watch_next=[
                {"title": "Earlier video", "yt_video_id": "abc123"},
                {"title": "Short one", "yt_url": "https://youtu.be/short1"},
            ],
            sources=[
                {"title": "Tool X launches", "publisher": "TechCrunch",
                 "url": "https://example.com/tc"},
            ],
        )
        self.assertTrue(out.startswith("DISCLAIMER"))
        self.assertLess(out.index("Watch next:"), out.index("Sources / References:"))
        self.assertIn("• Earlier video — https://youtu.be/abc123", out)
        self.assertIn("• Short one — https://youtu.be/short1", out)
        self.assertIn("• Tool X launches — TechCrunch — https://example.com/tc", out)

    def test_watch_next_dedupes_by_video_id(self):
        from agents.uploader import build_video_description

        out = build_video_description(
            BASE_DESCRIPTION,
            watch_next=[
                {"title": "Same", "yt_video_id": "dup1"},
                {"title": "Same again", "yt_video_id": "dup1"},
                {"title": "No id at all"},
            ],
        )
        self.assertEqual(out.count("youtu.be/dup1"), 1)
        self.assertNotIn("No id at all", out)

    def test_sources_render_without_publisher_or_url(self):
        from agents.uploader import build_video_description

        out = build_video_description(
            BASE_DESCRIPTION,
            sources=[{"title": "Trend signals: google_news", "publisher": "", "url": ""}],
        )
        self.assertIn("• Trend signals: google_news", out)
        self.assertNotIn("—  —", out)

    def test_rebuild_is_idempotent(self):
        from agents.uploader import build_video_description

        once = build_video_description(
            BASE_DESCRIPTION,
            watch_next=[{"title": "Earlier", "yt_video_id": "abc123"}],
            sources=[{"title": "Tool X launches", "publisher": "TC",
                      "url": "https://example.com/tc"}],
        )
        twice = build_video_description(
            once,
            watch_next=[{"title": "Earlier", "yt_video_id": "abc123"}],
            sources=[{"title": "Tool X launches", "publisher": "TC",
                      "url": "https://example.com/tc"}],
        )
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("Watch next:"), 1)
        self.assertEqual(twice.count("Sources / References:"), 1)

    def test_total_is_capped_at_5000_chars(self):
        from agents.uploader import MAX_DESCRIPTION_CHARS, build_video_description

        long_base = "DISCLAIMER: " + ("word " * 3000)  # ~15000 chars
        out = build_video_description(
            long_base,
            watch_next=[{"title": "Earlier", "yt_video_id": "abc123"}],
            sources=[{"title": "Tool X launches", "publisher": "TC",
                      "url": "https://example.com/tc"}],
        )
        self.assertLessEqual(len(out), MAX_DESCRIPTION_CHARS)
        self.assertTrue(out.startswith("DISCLAIMER:"))
        self.assertIn("Watch next:", out)
        self.assertIn("Sources / References:", out)


class TestReferencesFromBundle(unittest.TestCase):
    def test_real_sources_only(self):
        from agents.uploader import references_from_bundle

        refs = references_from_bundle(_make_ai_bundle())
        titles = [r["title"] for r in refs]
        self.assertIn("Tool X launches", titles)
        self.assertIn("Tool X pricing", titles)
        # Trend feeds are listed as one honest entry, not a fake article.
        self.assertTrue(any("google_news" in t for t in titles))
        self.assertTrue(any("google_trends_daily" in t for t in titles))
        tc = next(r for r in refs if r["title"] == "Tool X launches")
        self.assertEqual(tc["publisher"], "TechCrunch")
        self.assertEqual(tc["url"], "https://example.com/tc")

    def test_duplicate_urls_deduped_and_empty_titles_dropped(self):
        from agents.uploader import references_from_bundle

        bundle = {
            "sources": [
                {"claim": "Same article", "source": "TC", "url": "https://example.com/x"},
                {"claim": "Same article", "source": "TC", "url": "https://example.com/x"},
                {"claim": "", "source": "TC", "url": "https://example.com/y"},
                {"claim": "No URL article", "source": "Blog", "url": ""},
            ],
            "trend_sources": [],
        }
        refs = references_from_bundle(bundle)
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0]["url"], "https://example.com/x")
        self.assertEqual(refs[1]["title"], "No URL article")

    def test_no_bundle_means_no_references(self):
        from agents.uploader import references_from_bundle

        self.assertEqual(references_from_bundle(None), [])


class TestGetRelatedVideos(unittest.TestCase):
    def setUp(self):
        from db.sheets import save_video_record

        self.rows = []
        for video_id, title, pillar, status, created_at in [
            ("vid_a", "Tool X launches — full breakdown", "ai_everything", "pending_approval", 100),
            ("vid_b", "New AI model beats benchmarks", "ai_everything", "pending_approval", 200),
            ("vid_c", "Tool X launches", "finance_to_technical", "public", 300),
            ("vid_d", "Tool X launches", "ai_everything", "rejected", 400),
        ]:
            save_video_record({
                "video_id": video_id,
                "title": title,
                "topic": title,
                "yt_video_id": f"yt_{video_id}",
                "yt_url": f"https://youtu.be/yt_{video_id}",
                "pillar": pillar,
                "status": status,
                "created_at": created_at,
                "cost_usd": 0.0,
            })

    def test_scores_pillar_and_title_words(self):
        from db.sheets import get_related_videos

        related = get_related_videos(
            topic="Tool X launches", pillar="ai_everything", limit=5
        )
        ids = [r["yt_video_id"] for r in related]
        # A: pillar (+2) + "tool"+"launches" (+2) = 4. C: words only = 2
        # (newer than B). B: pillar only = 2. D: rejected, excluded.
        self.assertEqual(ids, ["yt_vid_a", "yt_vid_c", "yt_vid_b"])
        self.assertNotIn("yt_vid_d", ids)

    def test_excludes_given_ids_and_respects_limit(self):
        from db.sheets import get_related_videos

        related = get_related_videos(
            topic="Tool X launches", pillar="ai_everything", limit=1,
            exclude_yt_video_ids=["yt_vid_a"],
        )
        self.assertEqual(len(related), 1)
        self.assertNotEqual(related[0]["yt_video_id"], "yt_vid_a")

    def test_no_topic_falls_back_to_most_recent(self):
        from db.sheets import get_related_videos

        related = get_related_videos(topic="", pillar=None, limit=2)
        self.assertEqual(
            [r["yt_video_id"] for r in related], ["yt_vid_c", "yt_vid_b"]
        )


class TestCrossLinkDailyUploads(unittest.TestCase):
    def test_updates_long_description_with_shorts_links(self):
        from agents.uploader import cross_link_daily_uploads

        seen = {}

        def _fake_update(video_id, description):
            seen["video_id"] = video_id
            seen["description"] = description
            return True

        shorts = [
            {"title": "Short one", "yt_video_id": "short1", "status": "uploaded"},
            {"title": "Short two", "yt_video_id": "short2", "status": "uploaded"},
            {"title": "Short three", "status": "failed", "error": "boom"},
        ]
        with (
            mock.patch("agents.uploader.update_video_description", side_effect=_fake_update),
            mock.patch(
                "agents.uploader.get_related_videos",
                return_value=[{"title": "Earlier video", "yt_video_id": "earlier1",
                               "yt_url": "https://youtu.be/earlier1"}],
            ),
        ):
            updated, new_desc = cross_link_daily_uploads(
                "long123", BASE_DESCRIPTION, shorts, bundle=_make_ai_bundle()
            )
        self.assertTrue(updated)
        self.assertEqual(seen["video_id"], "long123")
        self.assertIn("• Short one — https://youtu.be/short1", seen["description"])
        self.assertIn("• Short two — https://youtu.be/short2", seen["description"])
        self.assertNotIn("Short three", seen["description"])  # failed: no ID, no link
        self.assertIn("• Earlier video — https://youtu.be/earlier1", seen["description"])
        self.assertIn("Sources / References:", seen["description"])
        self.assertIn("Tool X launches — TechCrunch", seen["description"])
        self.assertEqual(new_desc, seen["description"])

    def test_update_failure_returns_false_without_raising(self):
        from agents.uploader import cross_link_daily_uploads

        with (
            mock.patch("agents.uploader.update_video_description", return_value=False),
            mock.patch("agents.uploader.get_related_videos", return_value=[]),
        ):
            updated, new_desc = cross_link_daily_uploads(
                "long123", BASE_DESCRIPTION,
                [{"title": "S", "yt_video_id": "short1"}], bundle=None,
            )
        self.assertFalse(updated)
        self.assertIn("youtu.be/short1", new_desc)

    def test_update_exception_returns_false_without_raising(self):
        from agents.uploader import cross_link_daily_uploads

        with mock.patch(
            "agents.uploader.update_video_description",
            side_effect=RuntimeError("no service"),
        ):
            updated, _desc = cross_link_daily_uploads(
                "long123", BASE_DESCRIPTION,
                [{"title": "S", "yt_video_id": "short1"}], bundle=None,
            )
        self.assertFalse(updated)

    def test_simulated_id_raises(self):
        from agents.uploader import cross_link_daily_uploads

        with self.assertRaises(ValueError):
            cross_link_daily_uploads("yt_sim_abc", BASE_DESCRIPTION, [])


class TestUpdateVideoDescription(unittest.TestCase):
    def _fake_service(self, update_side_effect=None):
        service = mock.Mock()
        videos = mock.Mock()
        service.videos.return_value = videos
        videos.list.return_value.execute.return_value = {
            "items": [{
                "snippet": {
                    "title": "Tool X launches",
                    "tags": ["ai", "tools"],
                    "categoryId": "27",
                    "description": "old description",
                }
            }]
        }
        if update_side_effect is not None:
            videos.update.return_value.execute.side_effect = update_side_effect
        else:
            videos.update.return_value.execute.return_value = {}
        return service, videos

    def test_preserves_title_tags_category(self):
        from tools.youtube_api import update_video_description

        service, videos = self._fake_service()
        with mock.patch(
            "tools.youtube_api._get_youtube_service", return_value=service
        ):
            self.assertTrue(update_video_description("vid123", "new description"))
        body = videos.update.call_args.kwargs["body"]
        self.assertEqual(body["id"], "vid123")
        self.assertEqual(body["snippet"]["title"], "Tool X launches")
        self.assertEqual(body["snippet"]["tags"], ["ai", "tools"])
        self.assertEqual(body["snippet"]["categoryId"], "27")
        self.assertEqual(body["snippet"]["description"], "new description")

    def test_unknown_video_raises(self):
        from tools.youtube_api import update_video_description

        service = mock.Mock()
        service.videos.return_value.list.return_value.execute.return_value = {"items": []}
        with mock.patch(
            "tools.youtube_api._get_youtube_service", return_value=service
        ):
            with self.assertRaises(ValueError):
                update_video_description("vid123", "new description")

    def test_real_api_failure_returns_false(self):
        from tools.youtube_api import update_video_description

        # 403 is a permanent marker: no retry loop, one attempt, False.
        service, _videos = self._fake_service(
            update_side_effect=Exception("403 Forbidden")
        )
        with mock.patch(
            "tools.youtube_api._get_youtube_service", return_value=service
        ):
            self.assertFalse(update_video_description("vid123", "new description"))

    def test_simulated_id_raises(self):
        from tools.youtube_api import update_video_description

        with self.assertRaises(ValueError):
            update_video_description("yt_priv_abc", "new description")


class TestShortUploadLinksLongVideo(unittest.TestCase):
    def test_short_description_links_long_video_and_sources(self):
        from agents.uploader import run_upload_short

        captured = {}

        def _fake_upload_short(video_path, thumb_path, seo_data):
            captured["seo"] = dict(seo_data)
            return "short999"

        tmp = tempfile.TemporaryDirectory()
        video_path = Path(tmp.name) / "short.mp4"
        video_path.write_bytes(b"v" * 20000)
        with (
            mock.patch("agents.uploader.upload_short", side_effect=_fake_upload_short),
            mock.patch("agents.uploader.verify_video_exists", return_value=True),
            mock.patch("agents.uploader._probe_duration_sec", return_value=30.0),
            mock.patch("agents.uploader.save_short_record", return_value="short_abc"),
            mock.patch("agents.uploader.purge_local_media", return_value=None),
        ):
            result = run_upload_short(
                str(video_path), None, _make_short_script(),
                parent_topic="Tool X launches",
                bundle=_make_ai_bundle(),
                long_video={
                    "title": "Tool X launches — full breakdown",
                    "yt_video_id": "long123",
                    "yt_url": "https://youtu.be/long123",
                },
            )
        self.assertEqual(result.yt_video_id, "short999")
        self.assertEqual(result.format, "short")
        desc = captured["seo"]["description"]
        self.assertIn("Watch next:", desc)
        self.assertIn("Tool X launches — full breakdown — https://youtu.be/long123", desc)
        self.assertIn("Sources / References:", desc)
        self.assertIn("Tool X launches — TechCrunch — https://example.com/tc", desc)
        tmp.cleanup()


class TestShortUploadLinksRelated(unittest.TestCase):
    """Shorts get the previous-videos "Watch next" block (27 Nov 2026),
    like the long video — not just the same-day long-video link."""

    def _upload(self, related=None, related_lookup=None, long_video=None):
        from agents.uploader import run_upload_short

        captured = {}

        def _fake_upload_short(video_path, thumb_path, seo_data):
            captured["seo"] = dict(seo_data)
            return "short999"

        tmp = tempfile.TemporaryDirectory()
        video_path = Path(tmp.name) / "short.mp4"
        video_path.write_bytes(b"v" * 20000)
        patchers = [
            mock.patch("agents.uploader.upload_short", side_effect=_fake_upload_short),
            mock.patch("agents.uploader.verify_video_exists", return_value=True),
            mock.patch("agents.uploader._probe_duration_sec", return_value=30.0),
            mock.patch("agents.uploader.save_short_record", return_value="short_abc"),
            mock.patch("agents.uploader.purge_local_media", return_value=None),
        ]
        if related_lookup is not None:
            patchers.append(
                mock.patch(
                    "agents.uploader.get_related_videos",
                    side_effect=related_lookup,
                )
            )
        for p in patchers:
            p.start()
        try:
            result = run_upload_short(
                str(video_path), None, _make_short_script(),
                parent_topic="Tool X launches",
                bundle=_make_ai_bundle(),
                long_video=long_video,
                related=related,
            )
        finally:
            for p in patchers:
                p.stop()
            tmp.cleanup()
        return result, captured["seo"]["description"]

    def test_short_description_links_related_previous_videos(self):
        _, desc = self._upload(
            related=[{
                "title": "Earlier video",
                "yt_video_id": "earlier1",
                "yt_url": "https://youtu.be/earlier1",
            }],
            long_video={
                "title": "Tool X launches — full breakdown",
                "yt_video_id": "long123",
                "yt_url": "https://youtu.be/long123",
            },
        )
        self.assertIn("Watch next:", desc)
        self.assertIn(
            "Tool X launches — full breakdown — https://youtu.be/long123", desc
        )
        self.assertIn("Earlier video — https://youtu.be/earlier1", desc)
        # Same-day long video first, previous videos after it.
        self.assertLess(
            desc.index("https://youtu.be/long123"),
            desc.index("https://youtu.be/earlier1"),
        )

    def test_short_description_drops_simulated_related_ids(self):
        _, desc = self._upload(
            related=[
                {
                    "title": "Fake dry-run video",
                    "yt_video_id": "yt_sim_abc",
                    "yt_url": "https://youtu.be/yt_sim_abc",
                },
                {
                    "title": "Earlier video",
                    "yt_video_id": "earlier1",
                    "yt_url": "https://youtu.be/earlier1",
                },
            ],
            long_video="long123",
        )
        self.assertNotIn("yt_sim_abc", desc)
        self.assertIn("Earlier video — https://youtu.be/earlier1", desc)
        self.assertIn("https://youtu.be/long123", desc)

    def test_short_fetches_related_when_not_passed(self):
        calls = []

        def _lookup(**kwargs):
            calls.append(kwargs)
            return [{
                "title": "Earlier video",
                "yt_video_id": "earlier1",
                "yt_url": "https://youtu.be/earlier1",
            }]

        _, desc = self._upload(
            related=None, related_lookup=_lookup,
            long_video={
                "title": "Tool X launches — full breakdown",
                "yt_video_id": "long123",
                "yt_url": "https://youtu.be/long123",
            },
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["topic"], "Tool X launches")
        self.assertIn("long123", calls[0]["exclude_yt_video_ids"])
        self.assertIn("Earlier video — https://youtu.be/earlier1", desc)

    def test_short_related_lookup_failure_still_uploads(self):
        def _boom(**kwargs):
            raise RuntimeError("sheets down")

        result, desc = self._upload(
            related=None, related_lookup=_boom,
            long_video={
                "title": "Tool X launches — full breakdown",
                "yt_video_id": "long123",
                "yt_url": "https://youtu.be/long123",
            },
        )
        self.assertEqual(result.yt_video_id, "short999")
        # Long-video link survives a Sheets outage; no crash.
        self.assertIn(
            "Tool X launches — full breakdown — https://youtu.be/long123", desc
        )


if __name__ == "__main__":
    unittest.main()
