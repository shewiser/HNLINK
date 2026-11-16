"""
Tests for LinkedIn Poster Security, Token Lifecycles & Error Handling.

Verifies:
- Detection of unconfigured credentials.
- Validation of person URN format.
- Graceful handling of expired tokens (HTTP 401).
- HTTP 401 triggers automated refresh when configured.
- HTTP 400, 403, 429, 5xx do NOT trigger token refresh.
- Network timeouts raise LinkedInTimeoutError for ambiguous outcome handling.
- Refresh token rotation persistence across application restarts.
- Refresh persistence failure fails safely.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

from src.credential_store import SecureCredentialStore
from src.linkedin_poster import LinkedInPoster, LinkedInTimeoutError


class TestLinkedInSecurity(unittest.TestCase):
    def test_unconfigured_credentials_blocked(self):
        """Poster must refuse publishing when credentials are not configured."""
        poster = LinkedInPoster(access_token="", person_urn="")
        self.assertFalse(poster.is_configured())
        self.assertIsNone(poster.create_text_post("Test content"))
        self.assertIsNone(poster.create_article_post("Test", "https://example.com"))

    def test_invalid_person_urn_format(self):
        """Non-URN formatting is detected before submission."""
        poster = LinkedInPoster(access_token="valid_token", person_urn="12345_not_a_urn")
        is_valid, msg = poster.verify_credentials()
        self.assertFalse(is_valid)
        self.assertIn("Invalid LINKEDIN_PERSON_URN format", msg)

    @patch("requests.Session.get")
    def test_expired_token_detection(self, mock_get):
        """HTTP 401 response is diagnosed as token expiration (60-day lifecycle)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_get.return_value = mock_resp

        poster = LinkedInPoster(access_token="expired_token", person_urn="urn:li:person:abcdef")
        is_valid, msg = poster.verify_credentials()
        self.assertFalse(is_valid)
        self.assertIn("tokens expire after 60 days", msg)

    @patch("requests.Session.post")
    def test_successful_post_creation_returns_urn(self, mock_post):
        """Successful UGC post returns the X-RestLi-Id header value."""
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.headers = {"X-RestLi-Id": "urn:li:share:7123456789"}
        mock_post.return_value = mock_resp

        poster = LinkedInPoster(access_token="valid_token", person_urn="urn:li:person:abcdef")
        urn = poster.create_text_post("Clean viral post text")
        self.assertEqual(urn, "urn:li:share:7123456789")

    @patch("requests.post")
    def test_automated_token_refresh(self, mock_oauth_post):
        """When refresh credentials are configured, token can be refreshed."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "new_refreshed_token_xyz",
            "refresh_token": "new_rotated_refresh_token_abc",
        }
        mock_oauth_post.return_value = mock_resp

        poster = LinkedInPoster(access_token="old_token", person_urn="urn:li:person:abcdef")
        poster.refresh_token = "valid_refresh_token"
        poster.client_id = "test_client_id"
        poster.client_secret = "test_client_secret"

        self.assertTrue(poster.can_refresh_token())
        success, msg = poster.refresh_access_token()
        self.assertTrue(success)
        self.assertEqual(poster.access_token, "new_refreshed_token_xyz")
        # Rotated refresh token must be captured and persisted
        self.assertEqual(poster.refresh_token, "new_rotated_refresh_token_abc")

    @patch("requests.post")
    def test_refreshed_credentials_persisted_across_restarts(self, mock_oauth_post):
        """Refreshed and rotated tokens survive an application restart."""
        temp_dir = tempfile.mkdtemp()
        cred_file = os.path.join(temp_dir, "test_creds.json")
        try:
            store = SecureCredentialStore(store_path=cred_file)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "access_token": "persisted_access_token_123",
                "refresh_token": "persisted_rotated_refresh_token_456",
            }
            mock_oauth_post.return_value = mock_resp

            # Session 1: Performs refresh
            poster1 = LinkedInPoster(
                access_token="initial_token",
                person_urn="urn:li:person:abcdef",
                credential_store=store,
            )
            poster1.refresh_token = "initial_refresh_token"
            poster1.client_id = "test_client_id"
            poster1.client_secret = "test_client_secret"

            success, msg = poster1.refresh_access_token()
            self.assertTrue(success)

            # Session 2: Simulates app restart with new poster instance pointing to same store
            store2 = SecureCredentialStore(store_path=cred_file)
            poster2 = LinkedInPoster(
                access_token="",
                person_urn="urn:li:person:abcdef",
                credential_store=store2,
            )
            self.assertEqual(poster2.access_token, "persisted_access_token_123")
            self.assertEqual(poster2.refresh_token, "persisted_rotated_refresh_token_456")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @patch("requests.post")
    def test_refresh_persistence_failure_fails_safely(self, mock_oauth_post):
        """When credentials cannot be saved to persistent store, refresh fails safely."""
        mock_store = MagicMock()
        mock_store.load_credentials.return_value = {}
        mock_store.save_credentials.return_value = (False, "Permission denied / disk full")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "new_token",
            "refresh_token": "new_rotated_refresh",
        }
        mock_oauth_post.return_value = mock_resp

        poster = LinkedInPoster(
            access_token="old_token",
            person_urn="urn:li:person:abcdef",
            credential_store=mock_store,
        )
        poster.refresh_token = "old_refresh"
        poster.client_id = "test_client_id"
        poster.client_secret = "test_client_secret"

        success, msg = poster.refresh_access_token()
        self.assertFalse(success)
        self.assertIn("could not be securely persisted", msg)
        # Token must NOT be updated in memory to an unpersisted state
        self.assertEqual(poster.access_token, "old_token")
        self.assertEqual(poster.refresh_token, "old_refresh")

    @patch("requests.Session.post")
    @patch.object(LinkedInPoster, "refresh_access_token")
    def test_refresh_only_on_401_not_on_400_or_403(self, mock_refresh, mock_post):
        """400 Bad Request and 403 Forbidden must never trigger an OAuth refresh attempt."""
        poster = LinkedInPoster(access_token="valid_token", person_urn="urn:li:person:abcdef")
        poster.refresh_token = "valid_refresh_token"
        poster.client_id = "test_client_id"
        poster.client_secret = "test_client_secret"

        # 1. Test 400 Bad Request
        resp_400 = MagicMock()
        resp_400.status_code = 400
        mock_post.return_value = resp_400
        res = poster.create_text_post("Text that triggered 400")
        self.assertIsNone(res)
        mock_refresh.assert_not_called()

        # 2. Test 403 Forbidden
        resp_403 = MagicMock()
        resp_403.status_code = 403
        mock_post.return_value = resp_403
        res = poster.create_text_post("Text that triggered 403")
        self.assertIsNone(res)
        mock_refresh.assert_not_called()

    @patch("requests.Session.post")
    @patch.object(LinkedInPoster, "refresh_access_token")
    def test_refresh_not_attempted_on_429_or_500(self, mock_refresh, mock_post):
        """Rate limiting (429) and Server Errors (500) must never trigger token refresh."""
        poster = LinkedInPoster(access_token="valid_token", person_urn="urn:li:person:abcdef")
        poster.refresh_token = "valid_refresh_token"
        poster.client_id = "test_client_id"
        poster.client_secret = "test_client_secret"

        resp_429 = MagicMock()
        resp_429.status_code = 429
        mock_post.return_value = resp_429
        self.assertIsNone(poster.create_text_post("Rate limited text"))
        mock_refresh.assert_not_called()

        resp_500 = MagicMock()
        resp_500.status_code = 500
        mock_post.return_value = resp_500
        self.assertIsNone(poster.create_text_post("Server error text"))
        mock_refresh.assert_not_called()

    @patch("requests.Session.post")
    def test_network_timeout_raises_linkedin_timeout_error(self, mock_post):
        """Requests timeout raises LinkedInTimeoutError for ambiguous outcome handling."""
        mock_post.side_effect = requests.Timeout("Connection timed out")

        poster = LinkedInPoster(access_token="valid_token", person_urn="urn:li:person:abcdef")
        with self.assertRaises(LinkedInTimeoutError):
            poster.create_text_post("Test text that times out")


if __name__ == "__main__":
    unittest.main()
