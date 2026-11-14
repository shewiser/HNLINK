"""
SQLite-based history tracker and persistent pending-approval store.

Features:
- Configurable database path via HN_LINK_DB_PATH
- Preserves legacy database location (../post_history.db) if present
- Tracks posted stories to prevent duplicate publications
- Persistent pending-posts storage across application restarts
- Transactional state transitions (pending -> approved -> published / skipped / failed)
- Replay prevention for Telegram callbacks
- SQLite WAL mode and integrity verification
- Non-destructive backup and migration utilities
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


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

    def _get_connection(self) -> sqlite3.Connection:
        """Create a connection with WAL mode and busy timeout configured."""
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 15000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self):
        """Initialize database tables with schema migrations."""
        with self._get_connection() as conn:
            # 1. Posted stories table (deduplication)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS posted_stories (
                    hn_id INTEGER PRIMARY KEY,
                    title TEXT,
                    url TEXT,
                    linkedin_post_urn TEXT,
                    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

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

    # =========================================================================
    # POSTED STORIES / DEDUPLICATION
    # =========================================================================

    def is_posted(self, hn_id: int) -> bool:
        """Check if story ID has already been published."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM posted_stories WHERE hn_id = ?", (hn_id,)
            ).fetchone()
            return row is not None

    def mark_posted(self, hn_id: int, title: str, url: str, linkedin_post_urn: str = ""):
        """Record a story as published."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO posted_stories (hn_id, title, url, linkedin_post_urn, posted_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (hn_id, title, url, linkedin_post_urn, datetime.now(timezone.utc).isoformat()),
            )

    def get_all_posted_ids(self) -> Set[int]:
        """Retrieve set of all previously posted Hacker News story IDs."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT hn_id FROM posted_stories").fetchall()
            return {row["hn_id"] for row in rows}

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
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pending_posts (
                    post_id, hn_id, hn_title, hn_url, hn_score, hn_comments,
                    linkedin_text, image_url, image_data, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    post_id, hn_id, title, url, score, comments,
                    linkedin_text, image_url, image_data, now, now
                ),
            )
            return True

    def get_pending_post(self, post_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a pending post by ID."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM pending_posts WHERE post_id = ?", (post_id,)
            ).fetchone()
            if not row:
                return None
            return dict(row)

    def get_all_pending_posts(self, status: str = "pending") -> List[Dict[str, Any]]:
        """Retrieve all posts matching a specific status (default: pending)."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_posts WHERE status = ? ORDER BY created_at DESC", (status,)
            ).fetchall()
            return [dict(row) for row in rows]

    def claim_and_mark_published(self, post_id: str, linkedin_post_urn: str) -> bool:
        """
        Transactional transition: pending -> published.
        Inserts into posted_stories to prevent future duplicates.
        Returns False if post was not in 'pending' status (prevents replay attacks).
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            # Atomic update with condition status = 'pending'
            cursor = conn.execute(
                """
                UPDATE pending_posts
                SET status = 'published',
                    linkedin_post_urn = ?,
                    updated_at = ?
                WHERE post_id = ? AND status = 'pending'
                """,
                (linkedin_post_urn, now, post_id),
            )
            if cursor.rowcount == 0:
                # Post was already approved, skipped, or does not exist
                return False

            # Retrieve story details for deduplication record
            post = conn.execute(
                "SELECT hn_id, hn_title, hn_url FROM pending_posts WHERE post_id = ?",
                (post_id,),
            ).fetchone()

            if post and post["hn_id"]:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO posted_stories (hn_id, title, url, linkedin_post_urn, posted_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (post["hn_id"], post["hn_title"], post["hn_url"], linkedin_post_urn, now),
                )
            return True

    def claim_and_mark_skipped(self, post_id: str) -> bool:
        """
        Transactional transition: pending -> skipped.
        Returns False if post was not in 'pending' status.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
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

    def mark_failed(self, post_id: str, error_message: str) -> bool:
        """Record publishing failure for diagnostics while leaving eligible for retry."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE pending_posts
                SET status = 'failed',
                    error_message = ?,
                    updated_at = ?
                WHERE post_id = ?
                """,
                (error_message, now, post_id),
            )
            return cursor.rowcount > 0

    # =========================================================================
    # INTEGRITY, BACKUP & MIGRATION
    # =========================================================================

    def check_integrity(self) -> Tuple[bool, str]:
        """Validate SQLite database integrity."""
        try:
            with self._get_connection() as conn:
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

        with self._get_connection() as src_conn:
            with sqlite3.connect(dest_abs) as dest_conn:
                src_conn.backup(dest_conn)
        return dest_abs

    def migrate_from(self, source_db_path: str) -> Dict[str, int]:
        """
        Safely import history from an older/legacy database file.
        Creates a backup first, preserves existing records, and returns import counts.
        """
        source_abs = os.path.abspath(source_db_path)
        if not os.path.exists(source_abs):
            raise FileNotFoundError(f"Source database not found: {source_abs}")

        # Create safety backup of current target before migration
        backup_target = f"{self.db_path}.pre_migration_backup"
        self.backup(backup_target)

        imported_stories = 0
        imported_pending = 0

        with sqlite3.connect(source_abs) as src_conn:
            src_conn.row_factory = sqlite3.Row

            # Migrate posted_stories
            src_stories = src_conn.execute("SELECT * FROM posted_stories").fetchall()
            with self._get_connection() as dest_conn:
                for row in src_stories:
                    dest_conn.execute(
                        """
                        INSERT OR IGNORE INTO posted_stories (hn_id, title, url, linkedin_post_urn, posted_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (row["hn_id"], row["title"], row["url"], row["linkedin_post_urn"], row["posted_at"]),
                    )
                    imported_stories += 1

                # Migrate pending_posts if table exists in source
                has_pending = src_conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_posts'"
                ).fetchone()
                if has_pending:
                    src_pending = src_conn.execute("SELECT * FROM pending_posts").fetchall()
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
                                row["post_id"], row["hn_id"], row["hn_title"], row["hn_url"],
                                row["hn_score"], row["hn_comments"], row["linkedin_text"],
                                row["image_url"], row["image_data"], row["status"],
                                row["created_at"], row["updated_at"], row["linkedin_post_urn"],
                                row["error_message"]
                            ),
                        )
                        imported_pending += 1

        return {
            "imported_posted_stories": imported_stories,
            "imported_pending_posts": imported_pending,
            "backup_created": backup_target,
        }
