"""
Tests for CLI Publishing Safety (scripts/run_pipeline.py).

Verifies:
- Default invocation runs in dry-run mode without publishing.
- Non-interactive environments reject direct publishing attempts (fail closed).
- Interactive mode requires individual approval per post.
- Skipped or rejected posts are not published.
- Conflicting --publish and --dry-run flags are rejected by mutually exclusive group.
- --version displays version and exits cleanly.
"""

import io
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts.run_pipeline import main
from src.history_tracker import HistoryTracker


class TestCliSafety(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_cli.db")
        self._orig_db_path = os.environ.get("HN_LINK_DB_PATH")
        os.environ["HN_LINK_DB_PATH"] = self.db_path
        self.tracker = HistoryTracker(db_path=self.db_path)

    def tearDown(self):
        if self._orig_db_path is not None:
            os.environ["HN_LINK_DB_PATH"] = self._orig_db_path
        else:
            os.environ.pop("HN_LINK_DB_PATH", None)

    @patch("scripts.run_pipeline.HNFetcher")
    @patch("scripts.run_pipeline.ContentExtractor")
    @patch("scripts.run_pipeline.PostGenerator")
    @patch("scripts.run_pipeline.LinkedInPoster")
    def test_default_is_dry_run_no_publishing(
        self, mock_poster_cls, mock_gen_cls, mock_ext_cls, mock_fetcher_cls
    ):
        """Running with no arguments must execute in dry-run mode and never call LinkedInPoster."""
        mock_fetcher = mock_fetcher_cls.return_value
        mock_fetcher.get_mixed_stories.return_value = [
            {"id": 1001, "title": "Story 1", "url": "https://story1.com", "score": 100, "descendants": 20}
        ]
        mock_ext = mock_ext_cls.return_value
        mock_ext.extract.return_value = "Article content paragraph."

        mock_gen = mock_gen_cls.return_value
        mock_gen.generate.return_value = "Generated LinkedIn post."

        out_json = os.path.join(self.temp_dir, "test_output.json")
        test_args = ["run_pipeline.py", "--count", "1", "--output", out_json]

        with patch.object(sys, "argv", test_args):
            main()

        # LinkedInPoster._post must NOT be called in dry-run mode
        mock_poster_cls.return_value.create_article_post.assert_not_called()
        self.assertTrue(os.path.exists(out_json))

    @patch("scripts.run_pipeline.HNFetcher")
    @patch("scripts.run_pipeline.ContentExtractor")
    @patch("scripts.run_pipeline.PostGenerator")
    @patch("scripts.run_pipeline.LinkedInPoster")
    def test_explicit_dry_run_alone_no_publishing(
        self, mock_poster_cls, mock_gen_cls, mock_ext_cls, mock_fetcher_cls
    ):
        """Passing --dry-run explicitly executes in preview mode without publishing."""
        mock_fetcher = mock_fetcher_cls.return_value
        mock_fetcher.get_mixed_stories.return_value = [
            {"id": 1002, "title": "Dry Run Story", "url": "https://story2.com", "score": 150, "descendants": 30}
        ]
        mock_ext = mock_ext_cls.return_value
        mock_ext.extract.return_value = "Article content."
        mock_gen = mock_gen_cls.return_value
        mock_gen.generate.return_value = "Draft text."

        out_json = os.path.join(self.temp_dir, "test_dry_run_output.json")
        test_args = ["run_pipeline.py", "--dry-run", "--output", out_json]

        with patch.object(sys, "argv", test_args):
            main()

        mock_poster_cls.return_value.create_article_post.assert_not_called()
        self.assertTrue(os.path.exists(out_json))

    @patch("scripts.run_pipeline.sys.stdin.isatty", return_value=False)
    def test_non_interactive_publishing_fails_closed(self, mock_isatty):
        """Attempting to publish without an interactive TTY must exit with error."""
        test_args = ["run_pipeline.py", "--publish"]
        with patch.object(sys, "argv", test_args):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 1)

    @patch("scripts.run_pipeline.sys.stdin.isatty", return_value=True)
    @patch("builtins.input", side_effect=["n"])  # User skips post
    @patch("scripts.run_pipeline.HNFetcher")
    @patch("scripts.run_pipeline.ContentExtractor")
    @patch("scripts.run_pipeline.PostGenerator")
    @patch("scripts.run_pipeline.LinkedInPoster")
    def test_interactive_individual_skip_prevents_publishing(
        self, mock_poster_cls, mock_gen_cls, mock_ext_cls, mock_fetcher_cls, mock_input, mock_isatty
    ):
        """When user responds 'n', post is skipped and not sent to LinkedIn."""
        mock_fetcher = mock_fetcher_cls.return_value
        mock_fetcher.get_mixed_stories.return_value = [
            {"id": 2002, "title": "Story 2", "url": "https://story2.com", "score": 200, "descendants": 50}
        ]
        mock_ext = mock_ext_cls.return_value
        mock_ext.extract.return_value = "Article text."
        mock_gen = mock_gen_cls.return_value
        mock_gen.generate.return_value = "LinkedIn Draft."

        test_args = ["run_pipeline.py", "--publish"]
        with patch.object(sys, "argv", test_args):
            main()

        # LinkedIn poster must NOT have been called
        mock_poster_cls.return_value.create_article_post.assert_not_called()
        # Story must NOT be in posted_stories
        self.assertFalse(self.tracker.is_posted(2002))

    def test_conflicting_publish_and_dry_run_flags_rejected(self):
        """Passing both --publish and --dry-run must be rejected by argparse mutually exclusive group."""
        test_args = ["run_pipeline.py", "--publish", "--dry-run"]
        with patch.object(sys, "argv", test_args):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                with self.assertRaises(SystemExit) as cm:
                    main()
                self.assertEqual(cm.exception.code, 2)
                self.assertIn("not allowed with argument", mock_err.getvalue())

    def test_cli_version_flag(self):
        """Passing --version outputs the application identity and exits cleanly."""
        test_args = ["run_pipeline.py", "--version"]
        with patch.object(sys, "argv", test_args):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                with self.assertRaises(SystemExit) as cm:
                    main()
                self.assertEqual(cm.exception.code, 0)
                self.assertIn("HN-Link v2.0.0", mock_out.getvalue())


if __name__ == "__main__":
    unittest.main()
