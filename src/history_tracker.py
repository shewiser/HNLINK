"""
SQLite-based history tracker, persistent pending-approval store, and atomic idempotency engine.

Features:
- Configurable database path via HN_LINK_DB_PATH
- Preserves legacy database location (../post_history.db) if present
- Tracks posted stories to prevent duplicate publications
- Persistent idempotency claims table ensuring no publishing path bypasses deduplication
- Atomic state transitions (pending -> publishing -> published / skipped / failed / reconciliation_needed)
- Input bounds and string sanitization on story titles, URLs, texts, and error messages
- Connection lifecycle management with explicit closure to prevent resource leaks
- SQLite WAL mode and integrity verification
- Non-destructive backup and migration utilities
"""

from contextlib import contextmanager
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Input bounds (Fix L-1)
MAX_TITLE_CHARS = 500
MAX_URL_CHARS = 2048
MAX_POST_TEXT_CHARS = 25000
MAX_ERROR_CHARS = 2000
MAX_KEY_CHARS = 255


def _sanitize_str(val: Any, max_len: int) -> str:
    """Helper to convert to string and truncate within safe bounds."""
    if val is None:
        return ""
    return str(val)[:max_len]


def resolve_default_db_path() -> str:
    """
    Resolve the default database path in priority order:
    1. HN_LINK_DB_PATH environment variable
    2. Legacy database location (../post_history.db) if it exists on disk
    3. Project-local data directory (post_history.db)
    """
    env_path = os.environ.get("HN_LINK_DB_PATH", "").strip()
    if env_path:
        return os.path.abspath(env_path)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    legacy_path = os.path.abspath(os.path.join(repo_root, "..", "post_history.db"))
    if os.path.exists(legacy_path):
        return legacy_path

    # Standard safe location in repo root
    return os.path.join(repo_root, "post_history.db")


class HistoryTracker:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = os.path.abspath(db_path) if db_path else resolve_default_db_path()
        # Ensure parent directory exists safely
        parent_dir = os.path.dirname(self.db_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """
        Context manager that yields a configured SQLite connection and guarantees
        explicit connection closure and transaction commit/rollback.
        """
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA busy_timeout = 15000;")
            conn.execute("PRAGMA foreign_keys = ON;")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        """Initialize database tables and run non-destructive schema migrations."""
        with self._connection() as conn:
            # 1. Posted stories table (deduplication & publication states)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS posted_stories (
                    hn_id INTEGER PRIMARY KEY,
                    title TEXT,
                    url TEXT,
                    linkedin_post_urn TEXT,
                    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'published',
                    error_message TEXT
                );
            """)

            # Schema migration: Ensure 'status' and 'error_message' exist in legacy posted_stories
            cursor = conn.execute("PRAGMA table_info(posted_stories);")
            columns = {row["name"] for row in cursor.fetchall()}
            if "status" not in columns:
                conn.execute("ALTER TABLE posted_stories ADD COLUMN status TEXT DEFAULT 'published';")
            if "error_message" not in columns:
                conn.execute("ALTER TABLE posted_stories ADD COLUMN error_message TEXT;")

            # 2. Persistent pending posts table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_posts (
                    post_id TEXT PRIMARY KEY,
                    hn_id INTEGER,
                    hn_title TEXT,
                    hn_url TEXT,
                    hn_score INTEGER DEFAULT 0,
                    hn_comments INTEGER DEFAULT 0,
                    linkedin_text TEXT NOT NULL,
                    image_url TEXT,
                    image_data BLOB,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    linkedin_post_urn TEXT,
                    error_message TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_posts_status ON pending_posts(status);")

            # 3. Persistent idempotency claims table (Fix M-1)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS idempotency_claims (
                    idempotency_key TEXT PRIMARY KEY,
                    hn_id INTEGER,
                    title TEXT,
                    url TEXT,
                    linkedin_post_urn TEXT,
                    status TEXT DEFAULT 'publishing',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    error_message TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_hn_id ON idempotency_claims(hn_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_status ON idempotency_claims(status);")

    # =========================================================================
    # POSTED STORIES / DEDUPLICATION & WEBHOOK CLAIMS
    # =========================================================================

    def is_posted(self, hn_id: int) -> bool:
        """Check if story ID has already been published or is actively in flight."""
        if not hn_id:
            return False
        with self._connection() as conn:
            row = conn.execute(
                "SELECT status FROM posted_stories WHERE hn_id = ?", (hn_id,)
            ).fetchone()
            if not row:
                return False
            # 'failed' stories can be retried; 'published', 'publishing', or 'reconciliation_needed' cannot
            return row["status"] in ("published", "publishing", "reconciliation_needed")

    def claim_story_for_publishing(self, hn_id: int, title: str = "", url: str = "") -> Tuple[bool, str]:
        """
        Atomically claim a story for publishing to prevent concurrent or duplicate webhook publishing.
        Returns (success: bool, status_reason: str).
        """
        clean_title = _sanitize_str(title, MAX_TITLE_CHARS)
        clean_url = _sanitize_str(url, MAX_URL_CHARS)
        now = datetime.now(timezone.utc).isoformat()

        with self._connection() as conn:
            row = conn.execute(
                "SELECT status FROM posted_stories WHERE hn_id = ?", (hn_id,)
            ).fetchone()

            if row:
                current_status = row["status"]
                if current_status == "published":
                    return (False, "already_published")
                elif current_status == "publishing":
                    return (False, "currently_publishing")
                elif current_status == "reconciliation_needed":
                    return (False, "reconciliation_needed")
                elif current_status == "failed":
                    # Update status to publishing for retry
                    cursor = conn.execute(
                        """
                        UPDATE posted_stories
                        SET status = 'publishing', title = ?, url = ?, posted_at = ?, error_message = NULL
                        WHERE hn_id = ? AND status = 'failed'
                        """,
                        (clean_title, clean_url, now, hn_id),
                    )
                    return (cursor.rowcount > 0, "claimed" if cursor.rowcount > 0 else "conflict")

            # Not previously present, insert with status 'publishing'
            try:
                conn.execute(
                    """
                    INSERT INTO posted_stories (hn_id, title, url, linkedin_post_urn, posted_at, status)
                    VALUES (?, ?, ?, '', ?, 'publishing')
                    """,
                    (hn_id, clean_title, clean_url, now),
                )
                return (True, "claimed")
            except sqlite3.IntegrityError:
                # Concurrent worker inserted simultaneously
                return (False, "conflict")

    def mark_story_published(self, hn_id: int, title: str, url: str, linkedin_post_urn: str):
        """Finalize publication state of a story to 'published'."""
        clean_title = _sanitize_str(title, MAX_TITLE_CHARS)
        clean_url = _sanitize_str(url, MAX_URL_CHARS)
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO posted_stories (hn_id, title, url, linkedin_post_urn, posted_at, status, error_message)
                VALUES (?, ?, ?, ?, ?, 'published', NULL)
                ON CONFLICT(hn_id) DO UPDATE SET
                    title = excluded.title,
                    url = excluded.url,
                    linkedin_post_urn = excluded.linkedin_post_urn,
                    posted_at = excluded.posted_at,
                    status = 'published',
                    error_message = NULL
                """,
                (hn_id, clean_title, clean_url, linkedin_post_urn, now),
            )

    def mark_story_uncertain(self, hn_id: int, error_message: str):
        """Mark a story as requiring reconciliation (e.g. after network timeout during publishing)."""
        clean_error = _sanitize_str(error_message, MAX_ERROR_CHARS)
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE posted_stories
                SET status = 'reconciliation_needed',
                    error_message = ?,
                    posted_at = ?
                WHERE hn_id = ?
                """,
                (clean_error, now, hn_id),
            )

    def mark_story_failed(self, hn_id: int, error_message: str):
        """Mark publication attempt as failed."""
        clean_error = _sanitize_str(error_message, MAX_ERROR_CHARS)
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE posted_stories
                SET status = 'failed',
                    error_message = ?,
                    posted_at = ?
                WHERE hn_id = ?
                """,
                (clean_error, now, hn_id),
            )

    def mark_posted(self, hn_id: int, title: str, url: str, linkedin_post_urn: str = ""):
        """Record a story as published (compatibility helper)."""
        self.mark_story_published(hn_id, title, url, linkedin_post_urn)

    def get_all_posted_ids(self) -> Set[int]:
        """Retrieve set of all active or previously published Hacker News story IDs."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT hn_id FROM posted_stories WHERE status IN ('published', 'publishing', 'reconciliation_needed')"
            ).fetchall()
            return {row["hn_id"] for row in rows}

    # =========================================================================
    # IDEMPOTENCY CLAIMS (Fix M-1)
    # =========================================================================

    def claim_idempotency_key(
        self,
        idempotency_key: str,
        hn_id: Optional[int] = None,
        title: str = "",
        url: str = "",
    ) -> Tuple[bool, str]:
        """
        Atomically claim an idempotency key before external publishing.
        Enforces unique constraint across both HN and generic publications.
        Returns (success: bool, status_reason: str).
        """
        clean_key = _sanitize_str(idempotency_key, MAX_KEY_CHARS).strip()
        if not clean_key:
            return (False, "invalid_key")

        clean_title = _sanitize_str(title, MAX_TITLE_CHARS)
        clean_url = _sanitize_str(url, MAX_URL_CHARS)
        now = datetime.now(timezone.utc).isoformat()

        # If hn_id is provided, check posted_stories first
        if hn_id and hn_id > 0:
            claimed_hn, hn_reason = self.claim_story_for_publishing(hn_id, clean_title, clean_url)
            if not claimed_hn:
                return (False, hn_reason)

        with self._connection() as conn:
            row = conn.execute(
                "SELECT status FROM idempotency_claims WHERE idempotency_key = ?",
                (clean_key,),
            ).fetchone()

            if row:
                current_status = row["status"]
                if current_status in ("published", "publishing", "reconciliation_needed"):
                    return (False, current_status)
                elif current_status == "failed":
                    cursor = conn.execute(
                        """
                        UPDATE idempotency_claims
                        SET status = 'publishing', hn_id = ?, title = ?, url = ?, updated_at = ?, error_message = NULL
                        WHERE idempotency_key = ? AND status = 'failed'
                        """,
                        (hn_id, clean_title, clean_url, now, clean_key),
                    )
                    return (cursor.rowcount > 0, "claimed" if cursor.rowcount > 0 else "conflict")

            try:
                conn.execute(
                    """
                    INSERT INTO idempotency_claims (
                        idempotency_key, hn_id, title, url, linkedin_post_urn,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, '', 'publishing', ?, ?)
                    """,
                    (clean_key, hn_id, clean_title, clean_url, now, now),
                )
                return (True, "claimed")
            except sqlite3.IntegrityError:
                return (False, "conflict")

    def mark_claim_published(
        self,
        idempotency_key: str,
        linkedin_post_urn: str,
        hn_id: Optional[int] = None,
        title: str = "",
        url: str = "",
    ) -> bool:
        """Mark an idempotency claim as successfully published."""
        clean_key = _sanitize_str(idempotency_key, MAX_KEY_CHARS).strip()
        clean_title = _sanitize_str(title, MAX_TITLE_CHARS)
        clean_url = _sanitize_str(url, MAX_URL_CHARS)
        now = datetime.now(timezone.utc).isoformat()

        if hn_id and hn_id > 0:
            self.mark_story_published(hn_id, clean_title, clean_url, linkedin_post_urn)

        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE idempotency_claims
                SET status = 'published',
                    linkedin_post_urn = ?,
                    updated_at = ?,
                    error_message = NULL
                WHERE idempotency_key = ?
                """,
                (linkedin_post_urn, now, clean_key),
            )
            return cursor.rowcount > 0

    def mark_claim_uncertain(
        self,
        idempotency_key: str,
        error_message: str,
        hn_id: Optional[int] = None,
    ) -> bool:
        """Mark an idempotency claim as reconciliation_needed (e.g. after network timeout)."""
        clean_key = _sanitize_str(idempotency_key, MAX_KEY_CHARS).strip()
        clean_error = _sanitize_str(error_message, MAX_ERROR_CHARS)
        now = datetime.now(timezone.utc).isoformat()

        if hn_id and hn_id > 0:
            self.mark_story_uncertain(hn_id, clean_error)

        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE idempotency_claims
                SET status = 'reconciliation_needed',
                    error_message = ?,
                    updated_at = ?
                WHERE idempotency_key = ?
                """,
                (clean_error, now, clean_key),
            )
            return cursor.rowcount > 0

    def mark_claim_failed(
        self,
        idempotency_key: str,
        error_message: str,
        hn_id: Optional[int] = None,
    ) -> bool:
        """Mark an idempotency claim as failed."""
        clean_key = _sanitize_str(idempotency_key, MAX_KEY_CHARS).strip()
        clean_error = _sanitize_str(error_message, MAX_ERROR_CHARS)
        now = datetime.now(timezone.utc).isoformat()

        if hn_id and hn_id > 0:
            self.mark_story_failed(hn_id, clean_error)

        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE idempotency_claims
                SET status = 'failed',
                    error_message = ?,
                    updated_at = ?
                WHERE idempotency_key = ?
                """,
                (clean_error, now, clean_key),
            )
            return cursor.rowcount > 0

    def get_claim(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        """Retrieve an idempotency claim record by key."""
        clean_key = _sanitize_str(idempotency_key, MAX_KEY_CHARS).strip()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM idempotency_claims WHERE idempotency_key = ?",
                (clean_key,),
            ).fetchone()
            return dict(row) if row else None

    # =========================================================================
    # PERSISTENT PENDING APPROVALS
    # =========================================================================

    def save_pending_post(
        self,
        post_id: str,
        hn_id: int,
        title: str,
        url: str,
        score: int,
        comments: int,
        linkedin_text: str,
        image_url: Optional[str] = None,
        image_data: Optional[bytes] = None,
    ) -> bool:
        """Save a newly generated post draft awaiting approval."""
        clean_title = _sanitize_str(title, MAX_TITLE_CHARS)
        clean_url = _sanitize_str(url, MAX_URL_CHARS)
        clean_text = _sanitize_str(linkedin_text, MAX_POST_TEXT_CHARS)
        clean_image_url = _sanitize_str(image_url, MAX_URL_CHARS) if image_url else None
        now = datetime.now(timezone.utc).isoformat()

        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pending_posts (
                    post_id, hn_id, hn_title, hn_url, hn_score, hn_comments,
                    linkedin_text, image_url, image_data, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    post_id, hn_id, clean_title, clean_url, score, comments,
                    clean_text, clean_image_url, image_data, now, now
                ),
            )
            return True

    def get_pending_post(self, post_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a pending post by ID."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM pending_posts WHERE post_id = ?", (post_id,)
            ).fetchone()
            if not row:
                return None
            return dict(row)

    def get_all_pending_posts(self, status: str = "pending") -> List[Dict[str, Any]]:
        """Retrieve all posts matching a specific status (default: pending)."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_posts WHERE status = ? ORDER BY created_at DESC", (status,)
            ).fetchall()
            return [dict(row) for row in rows]

    def claim_for_publishing(self, post_id: str) -> bool:
        """
        Atomically transition: pending/failed -> publishing.
        Guarantees that only ONE worker can claim this post for publishing.
        Returns True if successfully claimed, False if already claimed/processed.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE pending_posts
                SET status = 'publishing',
                    updated_at = ?
                WHERE post_id = ? AND status IN ('pending', 'failed')
                """,
                (now, post_id),
            )
            return cursor.rowcount > 0

    def mark_published(self, post_id: str, linkedin_post_urn: str) -> bool:
        """
        Transition: publishing -> published.
        Inserts record into posted_stories to prevent future duplicate publications.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE pending_posts
                SET status = 'published',
                    linkedin_post_urn = ?,
                    updated_at = ?
                WHERE post_id = ? AND status IN ('publishing', 'pending')
                """,
                (linkedin_post_urn, now, post_id),
            )
            if cursor.rowcount == 0:
                return False

            post = conn.execute(
                "SELECT hn_id, hn_title, hn_url FROM pending_posts WHERE post_id = ?",
                (post_id,),
            ).fetchone()

            if post and post["hn_id"]:
                clean_title = _sanitize_str(post["hn_title"], MAX_TITLE_CHARS)
                clean_url = _sanitize_str(post["hn_url"], MAX_URL_CHARS)
                conn.execute(
                    """
                    INSERT INTO posted_stories (hn_id, title, url, linkedin_post_urn, posted_at, status)
                    VALUES (?, ?, ?, ?, ?, 'published')
                    ON CONFLICT(hn_id) DO UPDATE SET
                        title = excluded.title,
                        url = excluded.url,
                        linkedin_post_urn = excluded.linkedin_post_urn,
                        posted_at = excluded.posted_at,
                        status = 'published'
                    """,
                    (post["hn_id"], clean_title, clean_url, linkedin_post_urn, now),
                )
            return True

    def claim_and_mark_published(self, post_id: str, linkedin_post_urn: str) -> bool:
        """Compatibility method combining claim and publish."""
        return self.mark_published(post_id, linkedin_post_urn)

    def claim_and_mark_skipped(self, post_id: str) -> bool:
        """
        Transactional transition: pending -> skipped.
        Returns False if post was not in 'pending' status.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE pending_posts
                SET status = 'skipped',
                    updated_at = ?
                WHERE post_id = ? AND status = 'pending'
                """,
                (now, post_id),
            )
            return cursor.rowcount > 0

    def mark_publishing_failed(self, post_id: str, error_message: str) -> bool:
        """
        Transition: publishing -> failed.
        Records error message and releases lock so it can be retried or inspected.
        """
        clean_error = _sanitize_str(error_message, MAX_ERROR_CHARS)
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE pending_posts
                SET status = 'failed',
                    error_message = ?,
                    updated_at = ?
                WHERE post_id = ?
                """,
                (clean_error, now, post_id),
            )
            return cursor.rowcount > 0

    def mark_failed(self, post_id: str, error_message: str) -> bool:
        """Compatibility method for recording failures."""
        return self.mark_publishing_failed(post_id, error_message)

    def mark_publishing_uncertain(self, post_id: str, error_message: str) -> bool:
        """
        Transition: publishing -> reconciliation_needed.
        Used when an external call times out or drops connection ambiguously.
        Prevents automated replay without manual verification.
        """
        clean_error = _sanitize_str(error_message, MAX_ERROR_CHARS)
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE pending_posts
                SET status = 'reconciliation_needed',
                    error_message = ?,
                    updated_at = ?
                WHERE post_id = ? AND status = 'publishing'
                """,
                (clean_error, now, post_id),
            )
            return cursor.rowcount > 0

    # =========================================================================
    # INTEGRITY, BACKUP & MIGRATION
    # =========================================================================

    def check_integrity(self) -> Tuple[bool, str]:
        """Validate SQLite database integrity."""
        try:
            with self._connection() as conn:
                row = conn.execute("PRAGMA integrity_check;").fetchone()
                status = row[0] if row else "unknown"
                return (status == "ok", status)
        except Exception as e:
            return (False, str(e))

    def backup(self, destination_path: str) -> str:
        """Create a safe snapshot backup of the SQLite database."""
        dest_abs = os.path.abspath(destination_path)
        dest_dir = os.path.dirname(dest_abs)
        if dest_dir:
            os.makedirs(dest_dir, exist_ok=True)

        with self._connection() as src_conn:
            dest_conn = sqlite3.connect(dest_abs)
            try:
                src_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        return dest_abs

    def migrate_from(self, source_db_path: str) -> Dict[str, int]:
        """
        Safely import history from an older/legacy database file.
        Creates a backup first, preserves existing records, and returns import counts.
        """
        source_abs = os.path.abspath(source_db_path)
        if not os.path.exists(source_abs):
            raise FileNotFoundError(f"Source database not found: {source_abs}")

        backup_target = f"{self.db_path}.pre_migration_backup"
        self.backup(backup_target)

        imported_stories = 0
        imported_pending = 0

        src_conn = sqlite3.connect(source_abs)
        src_conn.row_factory = sqlite3.Row
        try:
            # Check source table structure
            has_stories = src_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='posted_stories'"
            ).fetchone()

            if has_stories:
                src_stories = src_conn.execute("SELECT * FROM posted_stories").fetchall()
                with self._connection() as dest_conn:
                    for row in src_stories:
                        dest_conn.execute(
                            """
                            INSERT OR IGNORE INTO posted_stories (hn_id, title, url, linkedin_post_urn, posted_at, status)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                row["hn_id"],
                                _sanitize_str(row["title"], MAX_TITLE_CHARS),
                                _sanitize_str(row["url"], MAX_URL_CHARS),
                                row["linkedin_post_urn"],
                                row["posted_at"],
                                row["status"] if "status" in row.keys() else "published",
                            ),
                        )
                        imported_stories += 1

            has_pending = src_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_posts'"
            ).fetchone()
            if has_pending:
                src_pending = src_conn.execute("SELECT * FROM pending_posts").fetchall()
                with self._connection() as dest_conn:
                    for row in src_pending:
                        dest_conn.execute(
                            """
                            INSERT OR IGNORE INTO pending_posts (
                                post_id, hn_id, hn_title, hn_url, hn_score, hn_comments,
                                linkedin_text, image_url, image_data, status, created_at, updated_at,
                                linkedin_post_urn, error_message
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                row["post_id"], row["hn_id"],
                                _sanitize_str(row["hn_title"], MAX_TITLE_CHARS),
                                _sanitize_str(row["hn_url"], MAX_URL_CHARS),
                                row["hn_score"], row["hn_comments"],
                                _sanitize_str(row["linkedin_text"], MAX_POST_TEXT_CHARS),
                                row["image_url"], row["image_data"], row["status"],
                                row["created_at"], row["updated_at"], row["linkedin_post_urn"],
                                _sanitize_str(row["error_message"], MAX_ERROR_CHARS)
                            ),
                        )
                        imported_pending += 1
        finally:
            src_conn.close()

        return {
            "imported_posted_stories": imported_stories,
            "imported_pending_posts": imported_pending,
            "backup_created": backup_target,
        }
