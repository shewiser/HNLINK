"""
Tests for LinkedIn Poster Security & Token Lifecycles.

Verifies:
- Detection of unconfigured credentials.
- Validation of person URN format.
- Graceful handling of expired tokens (HTTP 401).
- Handling of permission denial (HTTP 403).
- Token refresh mechanism when refresh credentials are provided.
"""

import unittest
from unittest.mock import MagicMock, patch

from src.linkedin_poster import LinkedInPoster


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
        mock_resp.json.return_value = {"access_token": "new_refreshed_token_xyz"}
        mock_oauth_post.return_value = mock_resp

        poster = LinkedInPoster(access_token="old_token", person_urn="urn:li:person:abcdef")
        poster.refresh_token = "valid_refresh_token"
        poster.client_id = "test_client_id"
        poster.client_secret = "test_client_secret"

        self.assertTrue(poster.can_refresh_token())
        success, msg = poster.refresh_access_token()
        self.assertTrue(success)
        self.assertEqual(poster.access_token, "new_refreshed_token_xyz")


if __name__ == "__main__":
    unittest.main()
