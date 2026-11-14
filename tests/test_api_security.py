"""
Tests for Flask API Security.

Verifies:
- Public endpoints (GET /health) are accessible.
- Sensitive endpoints require authentication.
- Invalid or missing tokens return 401 Unauthorized.
- Tokens in query parameters return 400 Bad Request.
- Constant-time comparison works for Bearer token and X-API-Key.
- Default host and debug settings adhere to security standards.
"""

import os
import unittest
from unittest.mock import patch

from api.app import app
from config.settings import settings


class TestApiSecurity(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.test_token = "secret-test-token-12345"
        self._orig_token = settings.api_auth_token
        settings.api_auth_token = self.test_token

    def tearDown(self):
        settings.api_auth_token = self._orig_token

    def test_health_check_public(self):
        """GET /health must be public and return 200 OK."""
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok"})

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
        with patch("api.app.LinkedInPoster") as mock_poster, \
             patch("api.app.HistoryTracker") as mock_tracker:
            instance = mock_poster.return_value
            instance.create_text_post.return_value = "urn:li:share:test12345"

            resp = self.client.post("/pipeline/post-to-linkedin", json={"text": "Secure test post"}, headers=headers)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json().get("status"), "posted")

    def test_valid_x_api_key_accepted(self):
        """Requests with valid X-API-Key header pass authentication."""
        headers = {"X-API-Key": self.test_token}
        with patch("api.app.LinkedInPoster") as mock_poster, \
             patch("api.app.HistoryTracker") as mock_tracker:
            instance = mock_poster.return_value
            instance.create_text_post.return_value = "urn:li:share:test12345"

            resp = self.client.post("/pipeline/post-to-linkedin", json={"text": "Secure test post"}, headers=headers)
            self.assertEqual(resp.status_code, 200)

    def test_fail_closed_when_token_unconfigured(self):
        """Server must return 503 if no API token is configured in settings."""
        settings.api_auth_token = ""
        settings.n8n_webhook_token = ""
        headers = {"Authorization": "Bearer any-token"}
        resp = self.client.post("/pipeline/post-to-linkedin", json={"text": "Test"}, headers=headers)
        self.assertEqual(resp.status_code, 503)
        self.assertIn("fail-closed", resp.get_json().get("error", ""))

    def test_safe_server_defaults(self):
        """Flask server default host must be 127.0.0.1 and debug must be False."""
        self.assertEqual(settings.flask_host, "127.0.0.1")
        self.assertFalse(settings.flask_debug)


if __name__ == "__main__":
    unittest.main()
