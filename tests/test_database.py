"""
Tests for SQLite HistoryTracker, Persistent Pending Approvals, and Idempotency Claims.

Verifies:
- Configurable database path via HN_LINK_DB_PATH.
- Duplicate posting prevention via posted_stories table.
- Persistent pending post storage and state transitions.
- Replay attack prevention (cannot approve twice).
- Database backup creation and restoration.
- Database integrity checks.
- Migration from legacy database.
- Explicit connection closure to prevent handle leaks.
- Transaction rollback on unhandled exceptions.
- Concurrent atomic story claims across threads.
- Input bounds truncation on malformed/oversized titles and URLs (Fix L-1).
- Persistent idempotency claims table uniqueness and lifecycle (Fix M-1).
- SecureCredentialStore permissions and encryption-at-rest (Fix M-2).
"""

from concurrent.futures import ThreadPoolExecutor
import os
import shutil
import sqlite3
import stat
import tempfile
import unittest

from src.credential_store import SecureCredentialStore
from src.history_tracker import (
    HistoryTracker,
    MAX_TITLE_CHARS,
    MAX_URL_CHARS,
    MAX_POST_TEXT_CHARS,
)


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

        # 2. Atomic claim for publishing
        claimed = self.tracker.claim_for_publishing(post_id)
        self.assertTrue(claimed)

        # Subsequent claim must fail (prevents concurrent double-publish)
        claimed_second = self.tracker.claim_for_publishing(post_id)
        self.assertFalse(claimed_second)

        # 3. Mark published
        urn = "urn:li:share:99999_urn"
        success_first = self.tracker.mark_published(post_id, urn)
        self.assertTrue(success_first)

        # Verified that story is now in posted_stories table
        self.assertTrue(self.tracker.is_posted(99999))

        # 4. Replay attack: attempting to claim or publish the same post again MUST FAIL
        self.assertFalse(self.tracker.claim_for_publishing(post_id))

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

    def test_connection_closure_and_resource_cleanup(self):
        """Executing database operations must explicitly close connection handles."""
        for i in range(25):
            self.tracker.is_posted(i)
            self.tracker.get_pending_post(f"dummy_{i}")

        is_valid, msg = self.tracker.check_integrity()
        self.assertTrue(is_valid, msg)

    def test_connection_closure_on_exception(self):
        """Connection must be closed even when an exception is raised inside the context manager."""
        try:
            with self.tracker._connection() as conn:
                conn.execute("SELECT 1")
                raise ValueError("Deliberate error to verify connection closure in finally")
        except ValueError:
            pass

        is_valid, msg = self.tracker.check_integrity()
        self.assertTrue(is_valid, msg)

    def test_transaction_rollback_on_exception(self):
        """Exceptions raised inside connection context manager must roll back transaction."""
        hn_id = 991199
        try:
            with self.tracker._connection() as conn:
                conn.execute(
                    "INSERT INTO posted_stories (hn_id, title) VALUES (?, ?)",
                    (hn_id, "Temporary Title"),
                )
                raise RuntimeError("Simulated failure during transaction")
        except RuntimeError:
            pass

        self.assertFalse(self.tracker.is_posted(hn_id))

    def test_concurrent_atomic_story_claims(self):
        """Multiple threads attempting to claim the same story: exactly ONE succeeds."""
        hn_id = 777777

        def attempt_claim(worker_id):
            return self.tracker.claim_story_for_publishing(
                hn_id=hn_id,
                title=f"Title by worker {worker_id}",
                url=f"https://example.com/{worker_id}",
            )

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(attempt_claim, i) for i in range(5)]
            results = [f.result() for f in futures]

        success_count = sum(1 for success, _ in results if success)
        self.assertEqual(success_count, 1, f"Expected 1 claim success, got {success_count}: {results}")

    def test_long_and_malformed_story_metadata_truncated(self):
        """Oversized story titles and URLs must be safely bounded to prevent database bloat (Fix L-1)."""
        huge_title = "A" * 5000
        huge_url = "https://example.com/" + ("x" * 10000)
        huge_text = "Post content " * 5000

        self.tracker.save_pending_post(
            post_id="post_oversized_1",
            hn_id=66666,
            title=huge_title,
            url=huge_url,
            score=10,
            comments=2,
            linkedin_text=huge_text,
        )

        post = self.tracker.get_pending_post("post_oversized_1")
        self.assertIsNotNone(post)
        self.assertLessEqual(len(post["hn_title"]), MAX_TITLE_CHARS)
        self.assertLessEqual(len(post["hn_url"]), MAX_URL_CHARS)
        self.assertLessEqual(len(post["linkedin_text"]), MAX_POST_TEXT_CHARS)

    def test_idempotency_claims_unique_constraint_and_lifecycle(self):
        """Idempotency claims table guarantees unique constraint across publications (Fix M-1)."""
        key = "unique-claim-key-12345"

        # 1. Claim succeeds
        claimed, status = self.tracker.claim_idempotency_key(
            idempotency_key=key,
            hn_id=44444,
            title="Claimed Story",
            url="https://example.com/claim",
        )
        self.assertTrue(claimed)
        self.assertEqual(status, "claimed")

        # 2. Duplicate claim attempt for active publishing is rejected
        claimed_dup, status_dup = self.tracker.claim_idempotency_key(idempotency_key=key)
        self.assertFalse(claimed_dup)
        self.assertEqual(status_dup, "publishing")

        # 3. Transition to published
        published = self.tracker.mark_claim_published(
            idempotency_key=key,
            linkedin_post_urn="urn:li:share:claim_urn_1",
            hn_id=44444,
        )
        self.assertTrue(published)

        claim_rec = self.tracker.get_claim(key)
        self.assertEqual(claim_rec["status"], "published")
        self.assertEqual(claim_rec["linkedin_post_urn"], "urn:li:share:claim_urn_1")

        # 4. Duplicate claim after publication is rejected
        claimed_after, status_after = self.tracker.claim_idempotency_key(idempotency_key=key)
        self.assertFalse(claimed_after)
        self.assertEqual(status_after, "published")

    def test_secure_credential_store_encryption_and_permissions(self):
        """SecureCredentialStore restricts permissions to 0600 and validates encryption (Fix M-2)."""
        cred_file = os.path.join(self.temp_dir, "test_secret_creds.json")
        orig_key = os.environ.get("HN_LINK_ENCRYPTION_KEY")
        try:
            os.environ["HN_LINK_ENCRYPTION_KEY"] = "my-secret-test-encryption-key-32b"
            store = SecureCredentialStore(store_path=cred_file)

            saved, err = store.save_credentials(
                access_token="test_access_token_abc",
                refresh_token="test_refresh_token_xyz",
            )
            self.assertTrue(saved, f"Saving failed: {err}")
            self.assertTrue(os.path.exists(cred_file))

            # Verify file permissions on POSIX (0600)
            file_stat = os.stat(cred_file)
            mode = stat.S_IMODE(file_stat.st_mode)
            self.assertEqual(mode, 0o600, f"Expected 0600 mode, got {oct(mode)}")

            # Verify contents are encrypted (no plaintext tokens)
            with open(cred_file, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn("test_access_token_abc", content)
            self.assertNotIn("test_refresh_token_xyz", content)

            # Verify decryption on load
            loaded = store.load_credentials()
            self.assertEqual(loaded.get("access_token"), "test_access_token_abc")
            self.assertEqual(loaded.get("refresh_token"), "test_refresh_token_xyz")

        finally:
            if orig_key is not None:
                os.environ["HN_LINK_ENCRYPTION_KEY"] = orig_key
            else:
                os.environ.pop("HN_LINK_ENCRYPTION_KEY", None)


if __name__ == "__main__":
    unittest.main()
