"""Unit tests for real YouTube OAuth2 in tools/youtube_api.py.

2026-11-25 (PR #4, feat/youtube-oauth2): _get_youtube_service() no
longer raises "OAuth2 not implemented" — it builds real OAuth2 user
credentials from YT_CLIENT_ID + YT_CLIENT_SECRET + YT_REFRESH_TOKEN
and refreshes the access token. These tests assert the new behaviour:

- missing env vars raise RuntimeError naming the missing var(s);
- a rejected refresh token (invalid_grant / revoked) raises
  RuntimeError loudly, never a fake success;
- with a mocked Google API client (test layer only — production code
  calls the real googleapiclient), upload_private returns the real
  video ID from the API, requires a bundle title, and calls
  videos.insert with privacyStatus=private;
- verify/publish/delete use the real API calls and report real
  outcomes.

No network call is made here: the Google client (build, Credentials,
MediaFileUpload) is mocked at the test layer only. No real secret,
token or video ID is used — all values below are test fixtures.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Add yt-agent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests._sheets_fake import install_fake_sheets

# Google Sheets is the data store now (DynamoDB removed 25 Nov 2026).
# Tests must never touch the real Sheets API — mock the client.
# (These tests previously relied on ALLOW_IN_MEMORY_CONFIG; the fake
# replaces that dev-only fallback. Installed in setUpModule; stopped in tearDownModule.)
_FAKE_SHEETS = None
_SHEETS_PATCHER = None


def setUpModule():
    global _FAKE_SHEETS, _SHEETS_PATCHER
    _FAKE_SHEETS, _SHEETS_PATCHER = install_fake_sheets()


def tearDownModule():
    if _SHEETS_PATCHER is not None:
        _SHEETS_PATCHER.stop()

import tools.youtube_api as yt_api
from tools.youtube_api import (
    delete_video,
    publish_public,
    upload_private,
    verify_video_exists,
)

YT_ENV = {
    "YT_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
    "YT_CLIENT_SECRET": "test-client-secret",
    "YT_REFRESH_TOKEN": "test-refresh-token",
}
REAL_VIDEO_ID = "dQw4w9WgXcQ"


def _mock_service():
    """A fake YouTube resource whose calls record args and return canned
    API-shaped responses. No network, no real Google client."""
    service = mock.MagicMock(name="youtube-service")
    insert_request = mock.MagicMock(name="insert-request")
    insert_request.next_chunk.return_value = (None, {"id": REAL_VIDEO_ID})
    service.videos.return_value.insert.return_value = insert_request
    service.videos.return_value.list.return_value.execute.return_value = {
        "items": [{"id": REAL_VIDEO_ID}]
    }
    service.videos.return_value.update.return_value.execute.return_value = {}
    service.videos.return_value.delete.return_value.execute.return_value = {}
    service.thumbnails.return_value.set.return_value.execute.return_value = {}
    return service


def _mock_credentials():
    """Fake google.oauth2.credentials.Credentials: refresh() succeeds
    and the credentials report valid=True."""
    creds = mock.MagicMock(name="credentials")
    creds.valid = True
    creds.refresh.return_value = None
    return creds


class TestYouTubeOAuth2(unittest.TestCase):
    """OAuth2 service construction — fail-loud on missing/revoked creds."""

    def test_missing_env_vars_raise_naming_them(self):
        """Every missing YT_* var must raise RuntimeError naming it —
        never a silent unauthenticated service."""
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in YT_ENV:
                os.environ.pop(name, None)
            with self.assertRaises(RuntimeError) as ctx:
                yt_api._get_youtube_service()
            for name in YT_ENV:
                self.assertIn(name, str(ctx.exception))

    def test_each_missing_env_var_named(self):
        """With only YT_REFRESH_TOKEN missing, the error names exactly it."""
        env = dict(YT_ENV)
        env.pop("YT_REFRESH_TOKEN")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                yt_api._get_youtube_service()
            self.assertIn("YT_REFRESH_TOKEN", str(ctx.exception))

    def test_revoked_refresh_token_raises_loudly(self):
        """invalid_grant (expired/revoked refresh token) must raise
        RuntimeError — never fall back to a fake ID or an unauthenticated
        service."""
        from google.auth.exceptions import RefreshError

        mock_creds = _mock_credentials()
        mock_creds.refresh.side_effect = RefreshError(
            "invalid_grant: Token has been expired or revoked."
        )
        with mock.patch.dict(os.environ, YT_ENV, clear=True):
            with mock.patch(
                "google.oauth2.credentials.Credentials",
                return_value=mock_creds,
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    yt_api._get_youtube_service()
                self.assertIn("refresh", str(ctx.exception).lower())
                # No credential value may leak into the error message.
                self.assertNotIn("test-refresh-token", str(ctx.exception))
                self.assertNotIn("test-client-secret", str(ctx.exception))

    def test_service_built_with_oauth_credentials_not_developer_key(self):
        """A valid refresh builds the service with credentials=... and
        never passes the client secret as developerKey."""
        mock_creds = _mock_credentials()
        mock_service = _mock_service()
        with mock.patch.dict(os.environ, YT_ENV, clear=True):
            with mock.patch(
                "google.oauth2.credentials.Credentials",
                return_value=mock_creds,
            ) as mock_creds_cls, mock.patch(
                "googleapiclient.discovery.build",
                return_value=mock_service,
            ) as mock_build:
                service = yt_api._get_youtube_service()
                self.assertIs(service, mock_service)
                mock_creds.refresh.assert_called_once()
                # Credentials built from the three env vars.
                _, kwargs = mock_creds_cls.call_args
                self.assertEqual(
                    kwargs["refresh_token"], YT_ENV["YT_REFRESH_TOKEN"]
                )
                self.assertEqual(kwargs["client_id"], YT_ENV["YT_CLIENT_ID"])
                self.assertEqual(
                    kwargs["client_secret"], YT_ENV["YT_CLIENT_SECRET"]
                )
                # build() called with OAuth credentials, no developerKey.
                _, build_kwargs = mock_build.call_args
                self.assertEqual(build_kwargs.get("credentials"), mock_creds)
                self.assertNotIn("developerKey", build_kwargs)


class TestYouTubeUploadPrivate(unittest.TestCase):
    """upload_private with a mocked service — real ID, private, titled."""

    def _temp_video(self):
        tmp = tempfile.NamedTemporaryFile(
            suffix=".mp4", delete=False, prefix="yt-oauth-test-"
        )
        tmp.write(b"fake-video-bytes" * 100)
        tmp.close()
        return tmp.name

    def test_upload_returns_real_video_id_private(self):
        """videos.insert is called private with the bundle title, and the
        real API video ID is returned — never a yt_sim_ ID."""
        video_path = self._temp_video()
        try:
            mock_service = _mock_service()
            with mock.patch.object(
                yt_api, "_get_youtube_service", return_value=mock_service
            ), mock.patch(
                "googleapiclient.http.MediaFileUpload",
                return_value=mock.MagicMock(),
            ):
                video_id = upload_private(
                    video_path,
                    None,
                    {
                        "title": "How Stripe Makes Money — Test",
                        "description": "Test description",
                        "tags": ["stripe", "fintech"],
                    },
                )
            self.assertEqual(video_id, REAL_VIDEO_ID)
            self.assertFalse(video_id.startswith("yt_sim_"))
            _, insert_kwargs = mock_service.videos.return_value.insert.call_args
            self.assertEqual(insert_kwargs["part"], "snippet,status")
            self.assertEqual(
                insert_kwargs["body"]["snippet"]["title"],
                "How Stripe Makes Money — Test",
            )
            self.assertEqual(
                insert_kwargs["body"]["status"]["privacyStatus"], "private"
            )
            self.assertEqual(
                insert_kwargs["body"]["snippet"]["tags"], ["stripe", "fintech"]
            )
        finally:
            Path(video_path).unlink(missing_ok=True)

    def test_upload_refuses_missing_title(self):
        """No title in seo_data → ValueError before any API call. No
        invented default title is used."""
        video_path = self._temp_video()
        try:
            mock_service = _mock_service()
            with mock.patch.object(
                yt_api, "_get_youtube_service", return_value=mock_service
            ):
                with self.assertRaises(ValueError):
                    upload_private(video_path, None, {"description": "x"})
                with self.assertRaises(ValueError):
                    upload_private(video_path, None, {"title": "   "})
            mock_service.videos.return_value.insert.assert_not_called()
        finally:
            Path(video_path).unlink(missing_ok=True)

    def test_upload_failure_raises_not_fake_id(self):
        """A failed resumable upload raises RuntimeError — never a fake ID."""
        video_path = self._temp_video()
        try:
            mock_service = _mock_service()
            failing = mock.MagicMock(name="failing-insert")
            failing.next_chunk.side_effect = RuntimeError("connection reset")
            mock_service.videos.return_value.insert.return_value = failing
            with mock.patch.object(
                yt_api, "_get_youtube_service", return_value=mock_service
            ), mock.patch(
                "googleapiclient.http.MediaFileUpload",
                return_value=mock.MagicMock(),
            ):
                with self.assertRaises(RuntimeError):
                    upload_private(
                        video_path, None, {"title": "Test Title"}
                    )
        finally:
            Path(video_path).unlink(missing_ok=True)


class TestYouTubeVerifyPublishDelete(unittest.TestCase):
    """verify/publish/delete call the real API shapes and report honestly."""

    def test_verify_video_exists_true_and_false(self):
        mock_service = _mock_service()
        with mock.patch.object(
            yt_api, "_get_youtube_service", return_value=mock_service
        ):
            self.assertTrue(verify_video_exists(REAL_VIDEO_ID))
            mock_service.videos.return_value.list.return_value.execute.return_value = {
                "items": []
            }
            self.assertFalse(verify_video_exists("other-real-id-123"))

    def test_verify_refuses_simulated_id(self):
        with self.assertRaises(ValueError):
            verify_video_exists("yt_sim_abc123")

    def test_publish_public_true_on_success_false_on_api_failure(self):
        mock_service = _mock_service()
        with mock.patch.object(
            yt_api, "_get_youtube_service", return_value=mock_service
        ):
            self.assertTrue(publish_public(REAL_VIDEO_ID))
            mock_service.videos.return_value.update.return_value.execute.side_effect = (
                RuntimeError("quota exceeded")
            )
            self.assertFalse(publish_public(REAL_VIDEO_ID))

    def test_delete_video_true_on_success_false_on_api_failure(self):
        mock_service = _mock_service()
        with mock.patch.object(
            yt_api, "_get_youtube_service", return_value=mock_service
        ):
            self.assertTrue(delete_video(REAL_VIDEO_ID))
            mock_service.videos.return_value.delete.return_value.execute.side_effect = (
                RuntimeError("not found")
            )
            self.assertFalse(delete_video(REAL_VIDEO_ID))


if __name__ == "__main__":
    unittest.main()
