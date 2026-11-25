"""Unit tests for Phase 6: Upload, Google Sheets, and Ephemeral Storage.

2026-11-25 rewrite: the old setUpClass called run_research()/run_script()
with no credentials and relied on the production mock/demo fallbacks to
fabricate a bundle, script, and a fake YouTube upload of a 29-byte dummy
file. Those fallbacks are removed. Tests now use explicit fixtures and
assert the uploader's fail-loud gates.
"""

import os
import sys
import unittest
from pathlib import Path

# Add yt-agent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from _fixtures import make_test_bundle, make_test_script
from _sheets_fake import install_fake_sheets
from agents.uploader import UploaderAgent, run_upload
from db.sheets import (
    cache_research,
    enqueue_topic,
    get_bot_requests,
    get_cached_research,
    get_next_topic,
    get_video_record,
    list_topics_queue,
    log_verification,
    record_pipeline_run,
    save_video_record,
    update_video_status,
    write_bot_request,
)
from db.setup_sheets import setup_all_sheets
from models.verification import CheckResult, VerificationReport
from storage.ephemeral import EphemeralWorkspace, purge_local_media, startup_orphan_sweep


def _fail_report():
    return VerificationReport(
        overall="fail",
        confidence=0.5,
        checks=[CheckResult(name="FACTUAL_ACCURACY", status="fail", issues=[])],
        suggested_fixes=[],
        model_used="test",
        deterministic_time_ms=10,
        llm_time_ms=0,
    )


class TestStorageAndUploader(unittest.TestCase):
    """Test suite covering Google Sheets persistence, ephemeral wipe, and YouTube private upload."""

    @classmethod
    def setUpClass(cls):
        cls.bundle = make_test_bundle()
        cls.script = make_test_script(cls.bundle)


# Google Sheets is the data store now (DynamoDB removed 25 Nov 2026).
# The in-memory fake Sheets service backs every Sheets read/write.
_FAKE_SHEETS, _SHEETS_PATCHER = install_fake_sheets()


def tearDownModule():
    _SHEETS_PATCHER.stop()

    def test_1_sheets_setup_fails_loudly_without_credentials(self):
        """setup_all_sheets() provisions real Google Sheets tabs. In CI
        there are no Sheets credentials, so it must raise RuntimeError
        instead of reporting success while creating nothing (the old
        test asserted a fake success dict)."""
        saved_id = os.environ.pop("GOOGLE_SHEETS_ID", None)
        saved_json = os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)
        try:
            with self.assertRaises(RuntimeError):
                setup_all_sheets()
        finally:
            if saved_id is not None:
                os.environ["GOOGLE_SHEETS_ID"] = saved_id
            if saved_json is not None:
                os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = saved_json

    def test_2_sheets_video_record_crud(self):
        """Verify save, get, and update status on yt_videos."""
        v_data = {
            "title": "Test Video Title",
            "topic": "Stripe Architecture",
            "yt_video_id": "test_yt_123",
            "yt_url": "https://youtu.be/test_yt_123",
            "status": "pending_approval",
        }
        v_id = save_video_record(v_data)
        self.assertIsNotNone(v_id)

        rec = get_video_record(v_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["title"], "Test Video Title")

        # Update to public
        updated = update_video_status(v_id, "public", published_at=1234567890)
        self.assertTrue(updated)
        rec_updated = get_video_record(v_id)
        self.assertEqual(rec_updated["status"], "public")

    def test_3_research_cache_ttl(self):
        """Verify research cache write and read."""
        t_hash = cache_research("Stripe Payments", '{"mock": "data"}', ttl_days=7)
        self.assertIsNotNone(t_hash)
        cached = get_cached_research("Stripe Payments")
        self.assertEqual(cached, '{"mock": "data"}')

    def test_4_topic_queue_flow(self):
        """Verify enqueue, list, and priority dequeue."""
        top_id = enqueue_topic("Adyen Global Expansion", pillar="finance_to_technical", priority=5)
        self.assertIsNotNone(top_id)

        queue = list_topics_queue(status="pending")
        self.assertTrue(any(t["topic_id"] == top_id for t in queue))

        next_t = get_next_topic()
        self.assertIsNotNone(next_t)
        self.assertEqual(next_t["status"], "running")

    def test_5_bot_request_message_bus(self):
        """Verify write and fetch bot requests."""
        req_id = write_bot_request("publish_video", {"video_id": "vid_123"})
        self.assertIsNotNone(req_id)
        pending = get_bot_requests(status="pending")
        self.assertTrue(any(r["request_id"] == req_id for r in pending))

    def test_6_ephemeral_purge_and_sweep(self):
        """Verify local media file deletion and orphan sweep."""
        # Create a dummy media file
        temp_media = Path("cache/renders/test_dummy_purge.mp4")
        temp_media.write_bytes(b"dummy_bytes_for_purge")
        self.assertTrue(temp_media.exists())

        # Purge
        deleted = purge_local_media(str(temp_media))
        self.assertEqual(deleted, 1)
        self.assertFalse(temp_media.exists())

        # Test orphan sweep
        orphaned = Path("cache/renders/orphan.mp4")
        orphaned.write_bytes(b"orphan")
        cleaned = startup_orphan_sweep(["cache/renders"])
        self.assertGreaterEqual(cleaned, 1)
        self.assertFalse(orphaned.exists())

    def test_7_uploader_requires_verification_report(self):
        """upload_and_register must refuse without a passing
        VerificationReport — the old signature took no report at all
        and recorded a hardcoded verification_score of 1.0."""
        sample_vid = Path("cache/renders/temp_upload_test.mp4")
        sample_vid.write_bytes(b"sample_video_container_bytes")
        uploader = UploaderAgent()
        # Missing the required verification_report argument.
        with self.assertRaises(TypeError):
            uploader.upload_and_register(
                video_path=str(sample_vid),
                thumb_path=None,
                script=self.script,
                cost_usd=0.08,
            )
        # A failing report must also be refused, and the local file
        # must be left intact (never purged on a refused upload).
        with self.assertRaises(Exception):
            uploader.upload_and_register(
                video_path=str(sample_vid),
                thumb_path=None,
                script=self.script,
                verification_report=_fail_report(),
                cost_usd=0.08,
            )
        self.assertTrue(sample_vid.exists())
        sample_vid.unlink(missing_ok=True)

    def test_8_log_verification_defaults_to_not_run(self):
        """A verification log entry with no overall/confidence must
        record 'not_run' / 0.0, never a default PASS / 1.0."""
        log_verification("vid_test_default", 1, {})
        # The fake Sheets service is the only store in CI; read the
        # record back through the public getter.
        from db.sheets import get_latest_verification_log
        rec = get_latest_verification_log("vid_test_default")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["overall"], "not_run")
        self.assertEqual(rec["confidence"], 0.0)


if __name__ == "__main__":
    unittest.main()
