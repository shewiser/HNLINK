"""
Security-hardened LinkedIn publisher via UGC Posts API (v2).

Features:
- Validates credential format and presence
- Safe verification against LinkedIn API without publishing
- Clear diagnostics for 60-day token expiration
- OAuth 2.0 refresh restricted strictly to HTTP 401 Unauthorized
- Never attempts refresh on 400, 403, 429, 5xx, or unrelated network errors
- Refresh token rotation persistence
- Distinguishes ambiguous network timeouts from definitive failures
- Safe error handling without leaking tokens or secrets in logs
- Text-only, article link preview, and image uploads
"""

import logging
from typing import Optional, Tuple

import requests

from config.settings import settings
from src.credential_store import SecureCredentialStore

logger = logging.getLogger(__name__)


class LinkedInPublishError(Exception):
    """Base exception for LinkedIn publishing errors."""
    pass


class LinkedInTimeoutError(LinkedInPublishError):
    """Raised when an external request to LinkedIn times out, leaving the outcome ambiguous."""
    pass


class LinkedInPoster:
    LINKEDIN_API_BASE = "https://api.linkedin.com/v2"
    LINKEDIN_OAUTH_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"

    def __init__(
        self,
        access_token: Optional[str] = None,
        person_urn: Optional[str] = None,
        credential_store: Optional[SecureCredentialStore] = None,
    ):
        self.credential_store = credential_store or SecureCredentialStore()
        stored_creds = self.credential_store.load_credentials()

        self.access_token = access_token or stored_creds.get("access_token") or settings.linkedin_access_token
        self.person_urn = person_urn or settings.linkedin_person_urn
        self.client_id = settings.linkedin_client_id
        self.client_secret = settings.linkedin_client_secret
        self.refresh_token = stored_creds.get("refresh_token") or settings.linkedin_refresh_token

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
        Only executed when LinkedIn confirms an HTTP 401 Unauthorized response.
        Captures and preserves rotated refresh tokens when returned.
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
                new_refresh = data.get("refresh_token")
                if new_token:
                    effective_refresh = new_refresh if new_refresh else self.refresh_token
                    # Persist rotated credentials to secure credential store
                    saved, err = self.credential_store.save_credentials(
                        access_token=new_token,
                        refresh_token=effective_refresh,
                    )
                    if not saved:
                        logger.error("Failed to securely persist refreshed credentials: %s", err)
                        return (
                            False,
                            f"OAuth token refresh succeeded, but rotated credentials could not be securely persisted ({err}). "
                            "Reauthorization required to prevent using an unpersisted token."
                        )

                    self.access_token = new_token
                    self._update_session_headers()
                    if new_refresh:
                        self.refresh_token = new_refresh
                        settings.linkedin_refresh_token = new_refresh
                        logger.info("LinkedIn refresh token rotated and persisted securely")
                    logger.info("Successfully refreshed and persisted LinkedIn OAuth credentials")
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
        except requests.Timeout as e:
            return (False, f"Network timeout during credential verification: {e}")
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

        except requests.Timeout as e:
            logger.error("LinkedIn upload registration timed out: %s", e)
            raise LinkedInTimeoutError(f"Upload registration timed out: {e}")
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
        except requests.Timeout as e:
            logger.error("LinkedIn image binary upload timed out: %s", e)
            raise LinkedInTimeoutError(f"Image binary upload timed out: {e}")
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
        """
        Send UGC post with strictly targeted OAuth token refresh ONLY upon confirmed 401.
        Never refreshes for 400, 403, 429, 5xx, or network errors.
        Retries at most once.
        """
        urn, status_code, is_timeout = self._post(payload)
        if is_timeout:
            raise LinkedInTimeoutError("LinkedIn UGC posting request timed out (ambiguous outcome)")

        if status_code == 401 and self.can_refresh_token():
            logger.info("Confirmed HTTP 401 Unauthorized; attempting automated OAuth token refresh...")
            success, msg = self.refresh_access_token()
            if success:
                logger.info("Retrying post submission once after successful token refresh...")
                urn, status_code, is_timeout = self._post(payload)
                if is_timeout:
                    raise LinkedInTimeoutError("LinkedIn UGC posting retry timed out (ambiguous outcome)")
                return urn
            else:
                logger.error("Token refresh failed: %s", msg)

        return urn

    def _post(self, payload: dict) -> Tuple[Optional[str], Optional[int], bool]:
        """
        Send the post request to LinkedIn UGC API.
        Returns (post_urn: Optional[str], status_code: Optional[int], is_timeout: bool).
        """
        try:
            resp = self.session.post(
                f"{self.LINKEDIN_API_BASE}/ugcPosts",
                json=payload,
                timeout=15,
            )
            if resp.status_code == 401:
                logger.error(
                    "LinkedIn API Error 401: Unauthorized. The access token has expired (60-day limit) or is invalid."
                )
                return (None, 401, False)
            elif resp.status_code == 403:
                logger.error("LinkedIn API Error 403: Forbidden. Verify application permissions (w_member_social).")
                return (None, 403, False)
            elif resp.status_code == 429:
                logger.error("LinkedIn API Error 429: Rate limit exceeded. Backing off.")
                return (None, 429, False)
            elif resp.status_code >= 400:
                logger.error("LinkedIn API Error %s: Request failed.", resp.status_code)
                return (None, resp.status_code, False)

            resp.raise_for_status()
            post_urn = resp.headers.get("X-RestLi-Id")
            logger.info("LinkedIn post successfully published (URN: %s)", post_urn)
            return (post_urn, resp.status_code, False)
        except requests.Timeout as e:
            logger.error("LinkedIn UGC posting timed out: %s", e)
            return (None, None, True)
        except requests.RequestException as e:
            status_code = getattr(e.response, "status_code", None) if hasattr(e, "response") else None
            logger.error("LinkedIn UGC posting failed (%s): %s", status_code, e)
            return (None, status_code, False)
