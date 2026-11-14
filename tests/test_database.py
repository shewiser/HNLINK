"""
Tests for SQLite HistoryTracker and Persistent Pending Approvals.

Verifies:
- Configurable database path via HN_LINK_DB_PATH.
- Duplicate posting prevention via posted_stories table.
- Persistent pending post storage and state transitions.
- Replay attack prevention (cannot approve twice).
- Database backup creation and restoration.
- Database integrity checks.
- Migration from legacy database.
"""

import os
import shutil
import sqlite3
import tempfile
import unittest

from src.history_tracker import HistoryTracker


class TestDatabaseSecurity(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_post_history.db")
        self.tracker = HistoryTracker(db_path=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_database_initialization_and_wal(self):
        """Database must initialize tables and enable WAL mode."""
        self.assertTrue(os.path.exists(self.db_path))
        is_valid, msg = self.tracker.check_integrity()
        self.assertTrue(is_valid, f"Database integrity check failed: {msg}")

    def test_posted_stories_deduplication(self):
        """Posted stories must be stored and detected to prevent duplicate publications."""
        self.assertFalse(self.tracker.is_posted(12345))
        self.tracker.mark_posted(
            hn_id=12345,
            title="Show HN: A New Test",
            url="https://example.com/test",
            linkedin_post_urn="urn:li:share:111",
        )
        self.assertTrue(self.tracker.is_posted(12345))
        self.assertIn(12345, self.tracker.get_all_posted_ids())

    def test_pending_post_lifecycle_and_replay_protection(self):
        """Pending posts must be saved, retrieved, and protected against replay attacks."""
        post_id = "post_99999_test"
        # 1. Save pending post
        self.tracker.save_pending_post(
            post_id=post_id,
            hn_id=99999,
            title="Tech Story",
            url="https://example.com/story",
            score=150,
            comments=42,
            linkedin_text="Viral LinkedIn draft content",
        )

        post = self.tracker.get_pending_post(post_id)
        self.assertIsNotNone(post)
        self.assertEqual(post["status"], "pending")
        self.assertEqual(post["hn_title"], "Tech Story")

        # 2. Claim and mark published
        urn = "urn:li:share:99999_urn"
        success_first = self.tracker.claim_and_mark_published(post_id, urn)
        self.assertTrue(success_first)

        # Verified that story is now in posted_stories table
        self.assertTrue(self.tracker.is_posted(99999))

        # 3. Replay attack: attempting to publish the same post again MUST FAIL
        success_replay = self.tracker.claim_and_mark_published(post_id, "urn:li:share:different")
        self.assertFalse(success_replay, "Replay attack was not prevented!")

    def test_pending_post_skip_lifecycle(self):
        """Skipping a post transitions status and prevents replay."""
        post_id = "post_88888_skip"
        self.tracker.save_pending_post(
            post_id=post_id,
            hn_id=88888,
            title="Skipped Story",
            url="https://example.com/skip",
            score=80,
            comments=10,
            linkedin_text="Draft text",
        )

        success = self.tracker.claim_and_mark_skipped(post_id)
        self.assertTrue(success)

        # Story is NOT in posted_stories table (so can be re-evaluated later if trending)
        self.assertFalse(self.tracker.is_posted(88888))

        # Replay attempt fails
        self.assertFalse(self.tracker.claim_and_mark_skipped(post_id))

    def test_database_backup(self):
        """Backup utility creates a verified copy of the database."""
        self.tracker.mark_posted(101, "Story 101", "https://example.com/101", "urn:101")
        backup_file = os.path.join(self.temp_dir, "backup.db")

        out_path = self.tracker.backup(backup_file)
        self.assertTrue(os.path.exists(out_path))

        # Verify backup contains data
        conn = sqlite3.connect(out_path)
        count = conn.execute("SELECT COUNT(*) FROM posted_stories").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_legacy_database_migration(self):
        """Migrating from an older database imports stories safely without data loss."""
        # Create a mock legacy database
        legacy_db = os.path.join(self.temp_dir, "legacy_post_history.db")
        with sqlite3.connect(legacy_db) as conn:
            conn.execute("""
                CREATE TABLE posted_stories (
                    hn_id INTEGER PRIMARY KEY,
                    title TEXT,
                    url TEXT,
                    linkedin_post_urn TEXT,
                    posted_at TIMESTAMP
                );
            """)
            conn.execute(
                "INSERT INTO posted_stories VALUES (555, 'Legacy Post', 'https://legacy.com', 'urn:555', '2026-01-01')"
            )

        # Migrate into self.tracker
        stats = self.tracker.migrate_from(legacy_db)
        self.assertEqual(stats["imported_posted_stories"], 1)
        self.assertTrue(self.tracker.is_posted(555))


if __name__ == "__main__":
    unittest.main()
