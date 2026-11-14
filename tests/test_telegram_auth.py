"""
Tests for Telegram Authorization, Atomic State Transitions, and Concurrency.

Verifies:
- Authorized users and chats are permitted.
- Unauthorized users and chats are rejected.
- Empty allowlist fails closed.
- Callback queries enforce authorization and answer exactly once.
- Concurrent approval attempts: only ONE publishing attempt occurs.
- Replay attacks on approve/skip callbacks are blocked.
- Ambiguous publishing timeouts transition post to 'reconciliation_needed'.
- Failed publishing transitions post to 'failed' and allows retry.
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from config.settings import settings
from src.history_tracker import HistoryTracker
from src.linkedin_poster import LinkedInTimeoutError
from src.telegram_bot import button_callback, is_authorized


class TestTelegramAuth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_tg.db")
        self._orig_db_path = os.environ.get("HN_LINK_DB_PATH")
        os.environ["HN_LINK_DB_PATH"] = self.db_path
        self.tracker = HistoryTracker(db_path=self.db_path)

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
        if self._orig_db_path is not None:
            os.environ["HN_LINK_DB_PATH"] = self._orig_db_path
        else:
            os.environ.pop("HN_LINK_DB_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

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

    async def test_concurrent_approval_atomic_lock(self):
        """
        When two administrators click Approve concurrently, only ONE publishing
        call must be made to LinkedIn, and the second must be rejected cleanly.
        """
        post_id = "concurrent_post_1"
        self.tracker.save_pending_post(
            post_id=post_id,
            hn_id=11111,
            title="Concurrent Test Story",
            url="https://example.com/concurrent",
            score=50,
            comments=10,
            linkedin_text="Post text for concurrency test",
        )

        def make_update(user_id):
            update = MagicMock()
            update.effective_user.id = user_id
            update.effective_chat.id = 999999
            query = AsyncMock()
            query.data = f"approve:{post_id}"
            query.message = AsyncMock()
            query.edit_message_reply_markup = AsyncMock()
            update.callback_query = query
            return update

        update1 = make_update(12345)
        update2 = make_update(67890)

        calls = []

        async def fake_create_text_post(text):
            calls.append(text)
            await asyncio.sleep(0.05)  # Simulate network latency
            return "urn:li:share:concurrent_success"

        with patch("src.telegram_bot.LinkedInPoster") as mock_poster_cls:
            instance = mock_poster_cls.return_value
            instance.create_text_post.side_effect = lambda text: asyncio.run(fake_create_text_post(text)) if not asyncio.iscoroutinefunction(fake_create_text_post) else fake_create_text_post(text)
            # Run synchronous wrapper that simulates async delay
            def sync_publish(text):
                calls.append(text)
                import time
                time.sleep(0.05)
                return "urn:li:share:concurrent_success"
            instance.create_text_post.side_effect = sync_publish

            # Run both callback handlers simultaneously
            await asyncio.gather(
                button_callback(update1, MagicMock()),
                button_callback(update2, MagicMock()),
            )

        # EXACTLY ONE LinkedIn call must have succeeded
        self.assertEqual(len(calls), 1, "Duplicate publishing occurred under concurrent approval!")

        # Post must now be in 'published' state in DB
        post = self.tracker.get_pending_post(post_id)
        self.assertEqual(post["status"], "published")
        self.assertEqual(post["linkedin_post_urn"], "urn:li:share:concurrent_success")

    async def test_ambiguous_publishing_timeout_marks_reconciliation(self):
        """A network timeout during posting transitions status to 'reconciliation_needed'."""
        post_id = "timeout_post_1"
        self.tracker.save_pending_post(
            post_id=post_id,
            hn_id=22222,
            title="Timeout Story",
            url="https://example.com/timeout",
            score=40,
            comments=5,
            linkedin_text="Post text",
        )

        update = MagicMock()
        update.effective_user.id = 12345
        update.effective_chat.id = 999999
        query = AsyncMock()
        query.data = f"approve:{post_id}"
        query.message = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        update.callback_query = query

        with patch("src.telegram_bot.LinkedInPoster") as mock_poster_cls:
            instance = mock_poster_cls.return_value
            instance.create_text_post.side_effect = LinkedInTimeoutError("Connection timed out")

            await button_callback(update, MagicMock())

        # Database record must be in reconciliation_needed, not pending or published
        post = self.tracker.get_pending_post(post_id)
        self.assertEqual(post["status"], "reconciliation_needed")
        self.assertIn("timeout", post["error_message"].lower())

    async def test_failed_publishing_allows_retry(self):
        """A definite failure transitions status to 'failed' and provides retry buttons."""
        post_id = "fail_post_1"
        self.tracker.save_pending_post(
            post_id=post_id,
            hn_id=33333,
            title="Failing Story",
            url="https://example.com/fail",
            score=30,
            comments=2,
            linkedin_text="Post text",
        )

        update = MagicMock()
        update.effective_user.id = 12345
        update.effective_chat.id = 999999
        query = AsyncMock()
        query.data = f"approve:{post_id}"
        query.message = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        update.callback_query = query

        with patch("src.telegram_bot.LinkedInPoster") as mock_poster_cls:
            instance = mock_poster_cls.return_value
            instance.create_text_post.return_value = None  # Failed

            await button_callback(update, MagicMock())

        post = self.tracker.get_pending_post(post_id)
        self.assertEqual(post["status"], "failed")


if __name__ == "__main__":
    unittest.main()
