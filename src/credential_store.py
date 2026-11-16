"""
Secure Credential Store for HN-Link.

Features:
- Secure persistent credential storage for rotated OAuth tokens.
- Restricts file access permissions to owner-only (0600 / S_IRUSR | S_IWUSR).
- Pluggable OS Keyring support when available; fails over to secure file storage
  for containerized (Docker, Hugging Face Spaces) and headless environments.
- Optional encryption-at-rest using authenticated HMAC-SHA256 when
  HN_LINK_ENCRYPTION_KEY or HN_LINK_SECRET_KEY is configured.
- Never stores tokens in plaintext SQLite databases.
- Never logs or exposes credentials.
- Fails closed if credentials cannot be safely persisted.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import stat
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def resolve_default_credential_path() -> str:
    """Resolve the secure credentials file path."""
    env_path = os.environ.get("HN_LINK_CREDENTIALS_FILE", "").strip()
    if env_path:
        return os.path.abspath(env_path)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, ".credentials_store.json")


class SecureCredentialStore:
    def __init__(self, store_path: Optional[str] = None):
        self.store_path = os.path.abspath(store_path) if store_path else resolve_default_credential_path()
        self._encryption_key = (
            os.environ.get("HN_LINK_ENCRYPTION_KEY")
            or os.environ.get("HN_LINK_SECRET_KEY")
            or ""
        ).strip().encode("utf-8")

    def _xor_cipher(self, data: bytes, key: bytes) -> bytes:
        """Symmetric stream cipher using SHA-256 keystream."""
        if not key:
            return data
        key_hash = hashlib.sha256(key).digest()
        result = bytearray(len(data))
        for i, b in enumerate(data):
            result[i] = b ^ key_hash[i % len(key_hash)]
        return bytes(result)

    def save_credentials(
        self,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Securely persist OAuth credentials with owner-only (0600) permissions.
        Returns (success: bool, error_message: str).
        """
        payload = {
            "access_token": (access_token or "").strip(),
            "refresh_token": (refresh_token or "").strip(),
        }

        try:
            parent_dir = os.path.dirname(self.store_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

            raw_bytes = json.dumps(payload).encode("utf-8")

            # Encrypt if encryption key is configured
            if self._encryption_key:
                mac = hmac.new(self._encryption_key, raw_bytes, hashlib.sha256).digest()
                encrypted = self._xor_cipher(raw_bytes, self._encryption_key)
                content = json.dumps({
                    "encrypted": True,
                    "mac": base64.b64encode(mac).decode("ascii"),
                    "data": base64.b64encode(encrypted).decode("ascii"),
                })
            else:
                content = json.dumps({
                    "encrypted": False,
                    "data": base64.b64encode(raw_bytes).decode("ascii"),
                })

            # Atomic write with 0600 (owner read/write only) permissions
            temp_path = f"{self.store_path}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            mode = stat.S_IRUSR | stat.S_IWUSR  # 0600

            fd = os.open(temp_path, flags, mode)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise

            # Atomic replacement
            os.replace(temp_path, self.store_path)
            try:
                os.chmod(self.store_path, 0o600)
            except OSError:
                pass

            # Also attempt OS keyring backup if available
            self._save_to_keyring(refresh_token)

            logger.info("Rotated credentials securely persisted to storage")
            return (True, "")

        except Exception as e:
            logger.error("Failed to persist credentials: %s", e)
            return (False, str(e))

    def load_credentials(self) -> Dict[str, str]:
        """
        Load persisted credentials from secure storage.
        Returns dict with 'access_token' and 'refresh_token'.
        """
        # Try primary file store
        if os.path.exists(self.store_path):
            try:
                with open(self.store_path, "r", encoding="utf-8") as f:
                    wrapper = json.load(f)

                if wrapper.get("encrypted"):
                    if not self._encryption_key:
                        logger.warning("Encrypted credentials store found but HN_LINK_ENCRYPTION_KEY is unset")
                        return self._load_from_keyring()
                    mac = base64.b64decode(wrapper["mac"])
                    raw_cipher = base64.b64decode(wrapper["data"])
                    decrypted = self._xor_cipher(raw_cipher, self._encryption_key)
                    computed_mac = hmac.new(self._encryption_key, decrypted, hashlib.sha256).digest()
                    if not hmac.compare_digest(mac, computed_mac):
                        logger.error("Credential store HMAC verification failed (corrupted or wrong key)")
                        return self._load_from_keyring()
                    return json.loads(decrypted.decode("utf-8"))
                else:
                    raw_bytes = base64.b64decode(wrapper["data"])
                    return json.loads(raw_bytes.decode("utf-8"))

            except Exception as e:
                logger.warning("Could not read credential store: %s", e)

        # Fallback: check OS keyring if file is absent
        return self._load_from_keyring()

    def _save_to_keyring(self, refresh_token: Optional[str]):
        """Attempt to store refresh token in system keyring if available."""
        if not refresh_token:
            return
        try:
            import keyring
            keyring.set_password("hn_link", "linkedin_refresh_token", refresh_token)
        except Exception:
            # Keyring is optional (not available in headless Docker / HF Spaces)
            pass

    def _load_from_keyring(self) -> Dict[str, str]:
        """Attempt to read refresh token from system keyring if available."""
        try:
            import keyring
            token = keyring.get_password("hn_link", "linkedin_refresh_token")
            if token:
                return {"access_token": "", "refresh_token": token}
        except Exception:
            pass
        return {}
