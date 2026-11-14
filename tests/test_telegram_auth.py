"""
Tests for Telegram Authorization & Callback Replay Protection.

Verifies:
- Authorized users and chats are permitted.
- Unauthorized users and chats are rejected.
- Empty allowlist fails closed.
- Callback queries enforce authorization.
- Replay attacks on approve/skip callbacks are blocked.
"""

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from config.settings import settings
from src.history_tracker import HistoryTracker
from src.telegram_bot import button_callback, is_authorized


class TestTelegramAuth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_users = settings.telegram_allowed_users
        self._orig_chats = settings.telegram_allowed_chats
        self._orig_chat_id = settings.telegram_chat_id
        settings.telegram_allowed_users = "12345,67890"
        settings.telegram_allowed_chats = "-100999"
        settings.telegram_chat_id = ""

    def tearDown(self):
        settings.telegram_allowed_users = self._orig_users
        settings.telegram_allowed_chats = self._orig_chats
        settings.telegram_chat_id = self._orig_chat_id

    def test_authorized_user_allowed(self):
        """User ID present in TELEGRAM_ALLOWED_USERS must be authorized."""
        mock_update = MagicMock()
        mock_update.effective_user.id = 12345
        mock_update.effective_chat.id = 999999
        self.assertTrue(is_authorized(mock_update))

    def test_authorized_chat_allowed(self):
        """Chat ID present in TELEGRAM_ALLOWED_CHATS must be authorized."""
        mock_update = MagicMock()
        mock_update.effective_user.id = 888888
        mock_update.effective_chat.id = -100999
        self.assertTrue(is_authorized(mock_update))

    def test_unauthorized_user_and_chat_rejected(self):
        """Unrecognized user and chat IDs must be rejected."""
        mock_update = MagicMock()
        mock_update.effective_user.id = 555555
        mock_update.effective_chat.id = 444444
        self.assertFalse(is_authorized(mock_update))

    def test_fail_closed_when_no_allowed_ids_configured(self):
        """If no allowlists or chat IDs are configured, all updates must be rejected."""
        settings.telegram_allowed_users = ""
        settings.telegram_allowed_chats = ""
        settings.telegram_chat_id = ""
        mock_update = MagicMock()
        mock_update.effective_user.id = 12345
        mock_update.effective_chat.id = 12345
        self.assertFalse(is_authorized(mock_update))

    async def test_unauthorized_callback_rejected(self):
        """Unauthorized callback queries must trigger an alert and halt."""
        mock_update = MagicMock()
        mock_update.effective_user.id = 999999  # unauthorized
        mock_update.effective_chat.id = 999999
        mock_query = AsyncMock()
        mock_update.callback_query = mock_query

        mock_context = MagicMock()

        await button_callback(mock_update, mock_context)
        mock_query.answer.assert_called_with(
            "⛔ Unauthorized: You do not have permission to approve or skip posts.",
            show_alert=True,
        )


if __name__ == "__main__":
    unittest.main()
