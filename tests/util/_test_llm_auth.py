import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lutris.util.llm_auth import GeminiOAuthProvider, LLMAuthUnavailable


class GeminiOAuthProviderTester(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.auth_dir = Path(self.tmpdir.name) / "llm"
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        self.provider = GeminiOAuthProvider()
        self.provider.token_path = self.auth_dir / "gemini-token.json"
        self.provider.client_secret_path = self.auth_dir / "gemini-client-secret.json"

    def write_secret(self, data):
        source_path = Path(self.tmpdir.name) / "source-client-secret.json"
        source_path.write_text(json.dumps(data), encoding="utf-8")
        return source_path

    def test_import_client_secret_copies_desktop_oauth_file(self):
        source_path = self.write_secret(
            {
                "installed": {
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                    "project_id": "lutris-test-project",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                },
            }
        )

        self.provider.import_client_secret(source_path)

        with self.provider.client_secret_path.open(encoding="utf-8") as secret_file:
            copied_data = json.load(secret_file)
        self.assertEqual(copied_data["installed"]["client_id"], "client-id")
        self.assertEqual(os.stat(self.provider.client_secret_path).st_mode & 0o777, 0o600)
        self.assertEqual(self.provider.load_project_id(), "lutris-test-project")

    def test_import_client_secret_rejects_invalid_json(self):
        source_path = Path(self.tmpdir.name) / "invalid.json"
        source_path.write_text("not json", encoding="utf-8")

        with self.assertRaisesRegex(LLMAuthUnavailable, "not valid JSON"):
            self.provider.import_client_secret(source_path)

        self.assertFalse(self.provider.client_secret_path.exists())

    def test_import_client_secret_rejects_web_oauth_file(self):
        source_path = self.write_secret(
            {
                "web": {
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                }
            }
        )

        with self.assertRaisesRegex(LLMAuthUnavailable, "desktop OAuth client"):
            self.provider.import_client_secret(source_path)

        self.assertFalse(self.provider.client_secret_path.exists())

    def test_import_client_secret_rejects_non_google_oauth_endpoints(self):
        source_path = self.write_secret(
            {
                "installed": {
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                    "auth_uri": "https://nextcloud.example.com/apps/oauth2/authorize",
                    "token_uri": "https://nextcloud.example.com/apps/oauth2/api/v1/token",
                }
            }
        )

        with self.assertRaisesRegex(LLMAuthUnavailable, "Google's OAuth endpoints"):
            self.provider.import_client_secret(source_path)

        self.assertFalse(self.provider.client_secret_path.exists())

    def test_load_access_token_refreshes_expired_token(self):
        self.provider.client_secret_path.write_text(
            json.dumps(
                {
                    "installed": {
                        "client_id": "client-id",
                        "client_secret": "client-secret",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                }
            ),
            encoding="utf-8",
        )
        self.provider.token_path.write_text(
            json.dumps(
                {
                    "access_token": "old-token",
                    "refresh_token": "refresh-token",
                    "expires_at": 1,
                }
            ),
            encoding="utf-8",
        )

        mock_response = mock.Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "access_token": "new-token",
            "expires_in": 3600,
        }

        with mock.patch("lutris.util.llm_auth.requests.post", return_value=mock_response) as request_post:
            access_token = self.provider.load_access_token()

        self.assertEqual(access_token, "new-token")
        request_post.assert_called_once()
        with self.provider.token_path.open(encoding="utf-8") as token_file:
            stored = json.load(token_file)
        self.assertEqual(stored["access_token"], "new-token")
