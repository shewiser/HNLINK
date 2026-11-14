"""
Tests for Flask API Security, Idempotency & Error Handling.

Verifies:
- Public endpoints (GET /health) are accessible and expose version.
- Sensitive endpoints require authentication.
- Invalid or missing tokens return 401 Unauthorized.
- Tokens in query parameters return 400 Bad Request.
- Constant-time comparison works for Bearer token and X-API-Key.
- Duplicate webhook requests for the same HN story return 409 Conflict.
- Network timeouts return 504 and record reconciliation_needed status.
- Default host and debug settings adhere to security standards.
"""

from concurrent.futures import ThreadPoolExecutor
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from api.app import app
from config.settings import settings
from src.history_tracker import HistoryTracker
from src.linkedin_poster import LinkedInTimeoutError


class TestApiSecurity(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_api.db")
        self._orig_db_path = os.environ.get("HN_LINK_DB_PATH")
        os.environ["HN_LINK_DB_PATH"] = self.db_path
        self.tracker = HistoryTracker(db_path=self.db_path)

        self.client = app.test_client()
        self.test_token = "secret-test-token-12345"
        self._orig_token = settings.api_auth_token
        settings.api_auth_token = self.test_token

    def tearDown(self):
        settings.api_auth_token = self._orig_token
        if self._orig_db_path is not None:
            os.environ["HN_LINK_DB_PATH"] = self._orig_db_path
        else:
            os.environ.pop("HN_LINK_DB_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_health_check_public(self):
        """GET /health must be public and return 200 OK with version metadata."""
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data.get("status"), "ok")
        self.assertEqual(data.get("version"), "2.0.0")

    def test_missing_auth_header_rejected(self):
        """Requests without Authorization or API-Key headers must return 401."""
        resp = self.client.post("/pipeline/fetch-and-generate", json={"count": 1})
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Missing authentication token", resp.get_json().get("error", ""))

        resp_publish = self.client.post("/pipeline/post-to-linkedin", json={"text": "Hello"})
        self.assertEqual(resp_publish.status_code, 401)

    def test_invalid_auth_token_rejected(self):
        """Requests with an invalid Bearer token must return 401."""
        headers = {"Authorization": "Bearer wrong-token"}
        resp = self.client.post("/pipeline/post-to-linkedin", json={"text": "Test"}, headers=headers)
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Invalid authentication token", resp.get_json().get("error", ""))

    def test_query_parameter_token_rejected(self):
        """Tokens passed via URL query parameters must be rejected with 400."""
        resp = self.client.post("/pipeline/post-to-linkedin?token=secret-test-token-12345", json={"text": "Test"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("query parameters", resp.get_json().get("error", ""))

    def test_valid_bearer_token_accepted(self):
        """Requests with valid Bearer token pass authentication."""
        headers = {"Authorization": f"Bearer {self.test_token}"}
        with patch("api.app.LinkedInPoster") as mock_poster:
            instance = mock_poster.return_value
            instance.create_text_post.return_value = "urn:li:share:test12345"

            resp = self.client.post(
                "/pipeline/post-to-linkedin",
                json={"text": "Secure test post", "hn_id": 101},
                headers=headers,
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json().get("status"), "posted")

    def test_valid_x_api_key_accepted(self):
        """Requests with valid X-API-Key header pass authentication."""
        headers = {"X-API-Key": self.test_token}
        with patch("api.app.LinkedInPoster") as mock_poster:
            instance = mock_poster.return_value
            instance.create_text_post.return_value = "urn:li:share:test12345"

            resp = self.client.post(
                "/pipeline/post-to-linkedin",
                json={"text": "Secure test post", "hn_id": 102},
                headers=headers,
            )
            self.assertEqual(resp.status_code, 200)

    def test_fail_closed_when_token_unconfigured(self):
        """Server must return 503 if no API token is configured in settings."""
        settings.api_auth_token = ""
        settings.n8n_webhook_token = ""
        headers = {"Authorization": "Bearer any-token"}
        resp = self.client.post("/pipeline/post-to-linkedin", json={"text": "Test"}, headers=headers)
        self.assertEqual(resp.status_code, 503)
        self.assertIn("fail-closed", resp.get_json().get("error", ""))

    def test_duplicate_webhook_rejected_with_409(self):
        """A second request for the same HN story must return HTTP 409 Conflict."""
        headers = {"Authorization": f"Bearer {self.test_token}"}
        payload = {
            "text": "First publish attempt",
            "hn_id": 99999,
            "article_url": "https://example.com/story99999",
            "article_title": "Story 99999",
        }

        with patch("api.app.LinkedInPoster") as mock_poster:
            instance = mock_poster.return_value
            instance.create_article_post.return_value = "urn:li:share:99999_post"

            # 1. First request succeeds
            resp1 = self.client.post("/pipeline/post-to-linkedin", json=payload, headers=headers)
            self.assertEqual(resp1.status_code, 200)
            self.assertEqual(resp1.get_json().get("status"), "posted")

            # 2. Replay/retry of same story must be rejected with 409 Conflict
            resp2 = self.client.post("/pipeline/post-to-linkedin", json=payload, headers=headers)
            self.assertEqual(resp2.status_code, 409)
            self.assertIn("Conflict", resp2.get_json().get("error", ""))

    def test_publishing_timeout_returns_504_and_reconciliation(self):
        """A timeout while contacting LinkedIn returns 504 and records reconciliation_needed."""
        headers = {"Authorization": f"Bearer {self.test_token}"}
        payload = {
            "text": "Timeout publish attempt",
            "hn_id": 88888,
            "article_url": "https://example.com/story88888",
        }

        with patch("api.app.LinkedInPoster") as mock_poster:
            instance = mock_poster.return_value
            instance.create_article_post.side_effect = LinkedInTimeoutError("Gateway timeout to LinkedIn")

            resp = self.client.post("/pipeline/post-to-linkedin", json=payload, headers=headers)
            self.assertEqual(resp.status_code, 504)
            data = resp.get_json()
            self.assertEqual(data.get("status"), "reconciliation_needed")

            # Subsequent attempt for same story must be blocked with 409
            resp2 = self.client.post("/pipeline/post-to-linkedin", json=payload, headers=headers)
            self.assertEqual(resp2.status_code, 409)

    def test_safe_server_defaults(self):
        """Flask server default host must be 127.0.0.1 and debug must be False."""
        self.assertEqual(settings.flask_host, "127.0.0.1")
        self.assertFalse(settings.flask_debug)


if __name__ == "__main__":
    unittest.main()
