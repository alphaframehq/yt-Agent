"""TubePilot AI — YouTube OAuth2 + resumable upload unit tests.

Validates fail-loud behaviour only — NO real YouTube call is made and
no video is uploaded:
1. A missing YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN raises
   RuntimeError naming the missing variable (never its value).
2. A token-refresh failure raises RuntimeError.
3. The service is built with OAuth2 credentials, never developerKey.
4. upload_private(): a missing file raises; an API error raises; an
   upload finishing without a video ID raises; a simulated yt_sim_
   ID is refused; a successful chunked upload returns the real video
   ID; retriable HTTP 500s are retried and then succeed, while a
   non-retriable 403 is not retried.
5. verify_video_exists() refuses simulated IDs; publish_public()
   raises without credentials.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from googleapiclient.errors import HttpError

import tools.youtube_api as yt
from tools.youtube_api import (
    _get_youtube_service,
    publish_public,
    upload_private,
    verify_video_exists,
)

# Dummy values only — never real credentials. Tests assert these
# values never appear in any raised error message.
_DUMMY_CREDS = {
    "YT_CLIENT_ID": "test-client-id",
    "YT_CLIENT_SECRET": "test-client-secret",
    "YT_REFRESH_TOKEN": "test-refresh-token",
}
_REAL_VIDEO_ID = "dQw4w9WgXcQ"


def _clear_yt_env():
    for key in _DUMMY_CREDS:
        os.environ.pop(key, None)


def _http_error(status, reason="Error"):
    resp = MagicMock()
    resp.status = status
    resp.reason = reason
    return HttpError(resp, b'{"error": {"message": "test error"}}')


def _fake_status(progress):
    status = MagicMock()
    status.progress.return_value = progress
    return status


class TestYouTubeOAuth2Service(unittest.TestCase):
    def test_missing_each_credential_raises_naming_it(self):
        for missing in _DUMMY_CREDS:
            with self.subTest(missing=missing):
                with patch.dict(os.environ, {}, clear=False):
                    _clear_yt_env()
                    for key, value in _DUMMY_CREDS.items():
                        if key != missing:
                            os.environ[key] = value
                    with self.assertRaises(RuntimeError) as ctx:
                        _get_youtube_service()
                    self.assertIn(missing, str(ctx.exception))
                    # No credential value may ever leak into the message.
                    for value in _DUMMY_CREDS.values():
                        self.assertNotIn(value, str(ctx.exception))

    def test_missing_all_credentials_raises_naming_all(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_yt_env()
            with self.assertRaises(RuntimeError) as ctx:
                _get_youtube_service()
            for name in _DUMMY_CREDS:
                self.assertIn(name, str(ctx.exception))

    def test_refresh_failure_raises(self):
        from google.auth.exceptions import RefreshError

        with patch.dict(os.environ, _DUMMY_CREDS, clear=False):
            with patch.object(yt, "Credentials") as mock_creds_cls:
                mock_creds = MagicMock()
                mock_creds.refresh.side_effect = RefreshError("invalid_grant")
                mock_creds_cls.return_value = mock_creds
                with self.assertRaises(RuntimeError) as ctx:
                    _get_youtube_service()
                self.assertIn("refresh", str(ctx.exception).lower())

    def test_service_built_with_oauth_credentials_never_developer_key(self):
        with patch.dict(os.environ, _DUMMY_CREDS, clear=False):
            with (
                patch.object(yt, "Credentials") as mock_creds_cls,
                patch.object(yt, "build") as mock_build,
            ):
                mock_creds = MagicMock()
                mock_creds.valid = True
                mock_creds_cls.return_value = mock_creds
                mock_build.return_value = MagicMock()
                service = _get_youtube_service()
                self.assertIs(service, mock_build.return_value)
                _, build_kwargs = mock_build.call_args
                self.assertEqual(build_kwargs.get("credentials"), mock_creds)
                self.assertNotIn("developerKey", build_kwargs)
                _, cred_kwargs = mock_creds_cls.call_args
                self.assertEqual(
                    cred_kwargs.get("refresh_token"), "test-refresh-token"
                )
                self.assertEqual(cred_kwargs.get("client_id"), "test-client-id")
                self.assertEqual(
                    cred_kwargs.get("client_secret"), "test-client-secret"
                )


class TestYouTubeUploadPrivate(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.write(fd, b"fake video bytes")
        os.close(fd)
        self.video = Path(path)

    def tearDown(self):
        self.video.unlink(missing_ok=True)

    def _service_with_request(self, request):
        service = MagicMock()
        service.videos.return_value.insert.return_value = request
        return service

    def test_missing_video_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            upload_private("/nonexistent/nope.mp4", None, {"title": "x"})

    def test_missing_credentials_raises_before_upload(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_yt_env()
            with self.assertRaises(RuntimeError):
                upload_private(str(self.video), None, {"title": "x"})

    def test_successful_chunked_upload_returns_real_id(self):
        request = MagicMock()
        request.next_chunk.side_effect = [
            (_fake_status(0.5), None),
            (None, {"id": _REAL_VIDEO_ID}),
        ]
        service = self._service_with_request(request)
        with (
            patch.object(yt, "_get_youtube_service", return_value=service),
            patch.object(yt, "MediaFileUpload") as mock_media,
        ):
            video_id = upload_private(
                str(self.video),
                None,
                {"title": "Test", "description": "d", "tags": ["a"]},
            )
        self.assertEqual(video_id, _REAL_VIDEO_ID)
        self.assertEqual(request.next_chunk.call_count, 2)
        _, media_kwargs = mock_media.call_args
        self.assertTrue(media_kwargs.get("resumable"))
        service.videos.return_value.insert.assert_called_once()
        _, insert_kwargs = service.videos.return_value.insert.call_args
        self.assertEqual(insert_kwargs.get("part"), "snippet,status")

    def test_api_error_raises_and_is_not_retried_when_non_retriable(self):
        request = MagicMock()
        request.next_chunk.side_effect = _http_error(403, "Forbidden")
        service = self._service_with_request(request)
        with (
            patch.object(yt, "_get_youtube_service", return_value=service),
            patch.object(yt, "MediaFileUpload"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                upload_private(str(self.video), None, {"title": "x"})
        self.assertIn("403", str(ctx.exception))
        self.assertEqual(request.next_chunk.call_count, 1)

    def test_empty_response_raises(self):
        request = MagicMock()
        request.next_chunk.side_effect = [(None, {})]
        service = self._service_with_request(request)
        with (
            patch.object(yt, "_get_youtube_service", return_value=service),
            patch.object(yt, "MediaFileUpload"),
        ):
            with self.assertRaises(RuntimeError):
                upload_private(str(self.video), None, {"title": "x"})

    def test_simulated_id_is_refused(self):
        request = MagicMock()
        request.next_chunk.side_effect = [(None, {"id": "yt_sim_123"})]
        service = self._service_with_request(request)
        with (
            patch.object(yt, "_get_youtube_service", return_value=service),
            patch.object(yt, "MediaFileUpload"),
        ):
            with self.assertRaises(RuntimeError):
                upload_private(str(self.video), None, {"title": "x"})

    def test_retriable_500_is_retried_then_succeeds(self):
        request = MagicMock()
        request.next_chunk.side_effect = [
            _http_error(500, "Internal Error"),
            (_fake_status(1.0), None),
            (None, {"id": _REAL_VIDEO_ID}),
        ]
        service = self._service_with_request(request)
        with (
            patch.object(yt, "_get_youtube_service", return_value=service),
            patch.object(yt, "MediaFileUpload"),
            patch("time.sleep"),
        ):
            video_id = upload_private(str(self.video), None, {"title": "x"})
        self.assertEqual(video_id, _REAL_VIDEO_ID)
        self.assertEqual(request.next_chunk.call_count, 3)


class TestYouTubeFailLoudHelpers(unittest.TestCase):
    def test_verify_rejects_simulated_id(self):
        with self.assertRaises(ValueError):
            verify_video_exists("yt_sim_abc")

    def test_publish_public_without_credentials_raises(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_yt_env()
            with self.assertRaises(RuntimeError):
                publish_public(_REAL_VIDEO_ID)


if __name__ == "__main__":
    unittest.main()
