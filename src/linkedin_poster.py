"""
Security-hardened LinkedIn publisher via UGC Posts API (v2).

Features:
- Validates credential format and presence
- Safe verification against LinkedIn API without publishing
- Clear diagnostics for 60-day token expiration
- Automated refresh token exchange if refresh credentials are provided
- Safe error handling without leaking tokens in logs
- Text-only, article link preview, and image uploads
"""

import logging
from typing import Optional, Tuple

import requests

from config.settings import settings

logger = logging.getLogger(__name__)


class LinkedInPoster:
    LINKEDIN_API_BASE = "https://api.linkedin.com/v2"
    LINKEDIN_OAUTH_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"

    def __init__(
        self,
        access_token: Optional[str] = None,
        person_urn: Optional[str] = None,
    ):
        self.access_token = access_token or settings.linkedin_access_token
        self.person_urn = person_urn or settings.linkedin_person_urn
        self.client_id = settings.linkedin_client_id
        self.client_secret = settings.linkedin_client_secret
        self.refresh_token = settings.linkedin_refresh_token

        self.session = requests.Session()
        self._update_session_headers()

    def _update_session_headers(self):
        """Update session headers with current access token."""
        self.session.headers.update({
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        })

    def is_configured(self) -> bool:
        """Check if minimum publishing credentials are populated."""
        return bool(self.access_token and self.person_urn)

    def can_refresh_token(self) -> bool:
        """Check if OAuth refresh credentials are configured."""
        return bool(self.refresh_token and self.client_id and self.client_secret)

    def refresh_access_token(self) -> Tuple[bool, str]:
        """
        Attempt to refresh the OAuth 2.0 access token using grant_type=refresh_token.
        LinkedIn only supports refresh tokens if the app requested offline_access
        and developer eligibility criteria are met.
        """
        if not self.can_refresh_token():
            return (
                False,
                "Automated token refresh is unavailable: LINKEDIN_REFRESH_TOKEN, LINKEDIN_CLIENT_ID, "
                "or LINKEDIN_CLIENT_SECRET is missing. Standard LinkedIn user access tokens expire after "
                "60 days and must be renewed at https://developer.linkedin.com.",
            )

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        try:
            resp = requests.post(
                self.LINKEDIN_OAUTH_TOKEN_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                new_token = data.get("access_token")
                if new_token:
                    self.access_token = new_token
                    self._update_session_headers()
                    logger.info("Successfully refreshed LinkedIn OAuth access token")
                    return (True, "Access token refreshed successfully")
            error_desc = resp.json().get("error_description", resp.text) if resp.content else "Unknown error"
            return (False, f"Token refresh failed ({resp.status_code}): {error_desc}")
        except Exception as e:
            return (False, f"Token refresh request failed: {e}")

    def verify_credentials(self) -> Tuple[bool, str]:
        """
        Verify credentials by querying LinkedIn's userinfo endpoint without publishing.
        Returns (is_valid, status_message).
        """
        if not self.is_configured():
            return (False, "Missing LINKEDIN_ACCESS_TOKEN or LINKEDIN_PERSON_URN in configuration")

        if not self.person_urn.startswith("urn:li:person:"):
            return (False, f"Invalid LINKEDIN_PERSON_URN format: '{self.person_urn}'. Expected 'urn:li:person:<id>'.")

        try:
            resp = self.session.get(f"{self.LINKEDIN_API_BASE}/userinfo", timeout=10)
            if resp.status_code == 200:
                return (True, "Credentials verified successfully")
            elif resp.status_code == 401:
                return (
                    False,
                    "LinkedIn access token is invalid or expired (tokens expire after 60 days). "
                    "Renew your access token at https://developer.linkedin.com and update LINKEDIN_ACCESS_TOKEN.",
                )
            elif resp.status_code == 403:
                return (
                    False,
                    "LinkedIn permissions error (403 Forbidden). Ensure your developer application "
                    "has the 'Share on LinkedIn' product and 'w_member_social' scope enabled.",
                )
            else:
                return (False, f"LinkedIn credential verification failed with status {resp.status_code}")
        except Exception as e:
            return (False, f"Network error during credential verification: {e}")

    def create_text_post(self, text: str) -> Optional[str]:
        """Create a simple text-only post. Returns post URN on success."""
        if not self.is_configured():
            logger.error("Cannot post to LinkedIn: Credentials not configured")
            return None

        payload = {
            "author": self.person_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        }
        return self._post_with_retry(payload)

    def create_article_post(
        self, text: str, article_url: str, article_title: str = ""
    ) -> Optional[str]:
        """Create a post with an article link preview. Returns post URN on success."""
        if not self.is_configured():
            logger.error("Cannot post to LinkedIn: Credentials not configured")
            return None

        media_item = {
            "status": "READY",
            "originalUrl": article_url,
        }
        if article_title:
            media_item["title"] = {"text": article_title}

        payload = {
            "author": self.person_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "ARTICLE",
                    "media": [media_item],
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        }
        return self._post_with_retry(payload)

    def create_image_post(self, text: str, image_data: bytes) -> Optional[str]:
        """
        Create a post with an uploaded image.
        Three-step UGC process: 1) Register upload, 2) Upload binary, 3) Publish post with asset URN.
        """
        if not self.is_configured():
            logger.error("Cannot post to LinkedIn: Credentials not configured")
            return None

        # Step 1: Register upload
        register_payload = {
            "registerUploadRequest": {
                "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                "owner": self.person_urn,
                "serviceRelationships": [
                    {
                        "relationshipType": "OWNER",
                        "identifier": "urn:li:userGeneratedContent",
                    }
                ],
            }
        }

        try:
            reg_resp = self.session.post(
                f"{self.LINKEDIN_API_BASE}/assets?action=registerUpload",
                json=register_payload,
                timeout=15,
            )
            reg_resp.raise_for_status()
            reg_data = reg_resp.json()

            upload_url = reg_data["value"]["uploadMechanism"][
                "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"
            ]["uploadUrl"]
            asset = reg_data["value"]["asset"]
            logger.info("LinkedIn asset upload registered successfully")

        except Exception as e:
            logger.error("LinkedIn upload registration failed: %s", e)
            return None

        # Step 2: Upload binary
        try:
            upload_resp = requests.put(
                upload_url,
                data=image_data,
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/octet-stream",
                },
                timeout=30,
            )
            upload_resp.raise_for_status()
            logger.info("LinkedIn image binary uploaded successfully")
        except Exception as e:
            logger.error("LinkedIn image binary upload failed: %s", e)
            return None

        # Step 3: Create post with media asset
        payload = {
            "author": self.person_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "IMAGE",
                    "media": [
                        {
                            "status": "READY",
                            "media": asset,
                        }
                    ],
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        }
        return self._post_with_retry(payload)

    def _post_with_retry(self, payload: dict) -> Optional[str]:
        """Send UGC post with automatic token refresh attempt on 401 if refresh is configured."""
        urn = self._post(payload)
        if urn is None and self.can_refresh_token():
            logger.info("Publishing failed; attempting automated OAuth token refresh...")
            success, msg = self.refresh_access_token()
            if success:
                logger.info("Retrying post submission after token refresh...")
                return self._post(payload)
            else:
                logger.error("Token refresh failed: %s", msg)
        return urn

    def _post(self, payload: dict) -> Optional[str]:
        """Send the post request to LinkedIn UGC API."""
        try:
            resp = self.session.post(
                f"{self.LINKEDIN_API_BASE}/ugcPosts",
                json=payload,
                timeout=15,
            )
            if resp.status_code == 401:
                logger.error(
                    "LinkedIn API Error 401: Unauthorized. The access token has expired (60-day limit) or is invalid. "
                    "Renew at https://developer.linkedin.com."
                )
                return None
            elif resp.status_code == 403:
                logger.error("LinkedIn API Error 403: Forbidden. Verify application permissions (w_member_social).")
                return None

            resp.raise_for_status()
            post_urn = resp.headers.get("X-RestLi-Id")
            logger.info("LinkedIn post successfully published (URN: %s)", post_urn)
            return post_urn
        except requests.RequestException as e:
            logger.error("LinkedIn UGC posting failed: %s", e)
            return None
