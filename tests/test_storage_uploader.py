"""Unit tests for Phase 6: Upload, DynamoDB, and Ephemeral Storage.

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

os.environ.setdefault("ALLOW_IN_MEMORY_CONFIG", "1")
os.environ.setdefault("ALLOW_IN_MEMORY_SECRETS", "1")

from _fixtures import make_test_bundle, make_test_script
from agents.uploader import UploaderAgent, run_upload
from db.dynamo import (
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
from db.setup_dynamo import setup_all_tables
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
    """Test suite covering DynamoDB persistence, ephemeral wipe, and YouTube private upload."""

    @classmethod
    def setUpClass(cls):
        cls.bundle = make_test_bundle()
        cls.script = make_test_script(cls.bundle)

    def test_1_dynamo_table_setup_call(self):
        """Verify setup_all_tables executes cleanly without crashing."""
        res = setup_all_tables()
        self.assertIn("status", res)

    def test_2_dynamo_video_record_crud(self):
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
        # The in-memory fallback store is the only store in CI; read
        # the record back through the module's store if present.
        from db.dynamo import _IN_MEMORY_STORE
        rec = _IN_MEMORY_STORE["yt_verification_log"].get("vid_test_default#1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["overall"], "not_run")
        self.assertEqual(rec["confidence"], 0.0)


if __name__ == "__main__":
    unittest.main()
