#!/usr/bin/env python3
"""
Security-hardened standalone pipeline script.

Safeguards:
- Dry-run is the mandatory DEFAULT mode.
- Publishing requires explicit --publish flag AND interactive terminal confirmation for each post.
- Non-interactive environments fail closed.
- Duplicate publishing prevented via HistoryTracker.
- Generated drafts are saved to pending_posts.json and persisted in SQLite.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from config.settings import settings
from src.content_extractor import ContentExtractor
from src.history_tracker import HistoryTracker
from src.hn_fetcher import HNFetcher
from src.linkedin_poster import LinkedInPoster
from src.post_generator import PostGenerator


def main():
    parser = argparse.ArgumentParser(
        description="HN-to-LinkedIn Pipeline (Security-Hardened)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Safety Notes:
  - By default, this script runs in DRY-RUN mode and will NOT publish to LinkedIn.
  - To initiate the interactive review and publishing process, pass --publish.
  - Each post requires explicit individual confirmation [y/N] in your terminal.
        """,
    )
    parser.add_argument(
        "--count",
        type=int,
        default=3,
        help="Number of posts to generate (default: 3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Preview posts and save to pending_posts.json without posting (DEFAULT)",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Enable interactive human-in-the-loop review to publish approved posts to LinkedIn",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="pending_posts.json",
        help="File path to save generated drafts (default: pending_posts.json)",
    )

    args = parser.parse_args()

    # Determine publishing mode: dry_run is default unless --publish is explicitly specified
    is_publishing_mode = bool(args.publish)

    print("=" * 60)
    print("HN-to-LinkedIn Bot (Security-Hardened CLI)")
    print(f"Mode: {'INTERACTIVE PUBLISHING' if is_publishing_mode else 'DRY RUN (Preview Only)'}")
    print("=" * 60)

    # Validate non-interactive safety
    if is_publishing_mode and not sys.stdin.isatty():
        print("\n❌ Safety Halt: Non-interactive terminal environment detected.")
        print("   Direct publishing requires an interactive TTY for human post-by-post confirmation.")
        print("   Run without --publish to generate drafts safely.")
        sys.exit(1)

    tracker = HistoryTracker()

    # Step 1: Fetch stories
    print("\n[1/4] Fetching stories from Hacker News...")
    fetcher = HNFetcher(
        top_count=settings.hn_top_stories_count,
        random_rising_count=settings.hn_random_rising_count,
        min_score=settings.hn_min_score,
    )
    stories = fetcher.get_mixed_stories()

    posted_ids = tracker.get_all_posted_ids()
    stories = [s for s in stories if s["id"] not in posted_ids]
    print(f"  Found {len(stories)} new stories (excluding {len(posted_ids)} previously posted)")

    if not stories:
        print("  No new stories to process. Exiting.")
        return

    # Step 2: Extract content
    print("\n[2/4] Extracting article content...")
    extractor = ContentExtractor(max_chars=settings.content_max_chars)
    stories_with_content = []
    for story in stories:
        url = story.get("url", "")
        if not url:
            continue
        content = extractor.extract(url)
        if content:
            story["content"] = content
            stories_with_content.append(story)
            print(f"  [OK] {story['title'][:60]}...")
        else:
            print(f"  [SKIP] Could not extract text: {story['title'][:60]}...")

    print(f"  Extracted clean content from {len(stories_with_content)} stories")

    # Step 3: Generate LinkedIn posts
    print("\n[3/4] Generating LinkedIn posts with AI...")
    generator = PostGenerator()
    generated_posts = []
    for story in stories_with_content:
        if len(generated_posts) >= args.count:
            break
        post_text = generator.generate(
            article_text=story["content"],
            hn_title=story["title"],
            hn_url=story["url"],
            hn_score=story.get("score", 0),
            hn_comments=story.get("descendants", 0),
        )
        if post_text:
            post_id = f"cli_{story['id']}_{len(generated_posts) + 1}"
            post_record = {
                "post_id": post_id,
                "hn_id": story["id"],
                "hn_title": story["title"],
                "hn_url": story["url"],
                "hn_score": story.get("score", 0),
                "hn_comments": story.get("descendants", 0),
                "linkedin_text": post_text,
            }
            generated_posts.append(post_record)

            # Persist draft to SQLite so it can also be tracked
            tracker.save_pending_post(
                post_id=post_id,
                hn_id=story["id"],
                title=story["title"],
                url=story["url"],
                score=story.get("score", 0),
                comments=story.get("descendants", 0),
                linkedin_text=post_text,
            )
            print(f"  [OK] {story['title'][:50]}...")
        else:
            print(f"  [FAIL] Generation failed for: {story['title'][:50]}...")

    print(f"  Generated {len(generated_posts)} posts")

    # Step 4: Preview or Individual Interactive Approval
    if not is_publishing_mode:
        print("\n[4/4] DRY RUN PREVIEWS (No content will be published)")
        for i, post in enumerate(generated_posts):
            print(f"\n{'=' * 60}")
            print(f"Draft #{i + 1}: {post['hn_title']}")
            print(f"Source: {post['hn_url']}")
            print(f"HN Engagement: {post['hn_score']} points · {post['hn_comments']} comments")
            print(f"{'=' * 60}")
            print(post["linkedin_text"])
            print()

        # Save to output JSON
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(generated_posts, f, indent=2)
        print(f"\n✅ Dry run complete. Saved {len(generated_posts)} post drafts to {args.output}")
        print("   To publish with interactive human review, run with: --publish")
        return

    # Interactive Publishing Workflow
    print("\n[4/4] INTERACTIVE PUBLISHING WORKFLOW (Individual Confirmation Required)")
    poster = LinkedInPoster()
    published_count = 0

    for i, post in enumerate(generated_posts):
        print(f"\n{'=' * 60}")
        print(f"Post {i + 1} of {len(generated_posts)}: {post['hn_title']}")
        print(f"HN: {post['hn_score']} pts · {post['hn_comments']} comments")
        print(f"{'=' * 60}")
        print(post["linkedin_text"])
        print(f"{'=' * 60}")

        # Check deduplication once more before prompting
        if tracker.is_posted(post["hn_id"]):
            print("⚠️ Notice: This story was already marked as posted in database. Skipping.")
            continue

        try:
            choice = input("\nPublish this post to LinkedIn now? [y/N/q(uit)]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted by user.")
            break

        if choice in ("y", "yes"):
            print("  -> Publishing to LinkedIn...")
            post_urn = poster.create_article_post(
                text=post["linkedin_text"],
                article_url=post["hn_url"],
                article_title=post["hn_title"],
            )
            if post_urn:
                tracker.claim_and_mark_published(post["post_id"], post_urn)
                published_count += 1
                print(f"  ✅ Published! Post URN: {post_urn}")
            else:
                tracker.mark_failed(post["post_id"], "LinkedIn publication failed")
                print("  ❌ Failed to publish post to LinkedIn.")
        elif choice in ("q", "quit"):
            print("  Quit command received. Halting publication queue.")
            break
        else:
            tracker.claim_and_mark_skipped(post["post_id"])
            print("  ⏭️ Post skipped by user.")

    print(f"\nWorkflow finished. Total posts published to LinkedIn: {published_count}")


if __name__ == "__main__":
    main()
