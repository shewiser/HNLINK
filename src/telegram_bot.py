"""
Security-hardened Telegram bot for HN-to-LinkedIn approval workflow.

Features:
- Strict authorization allowlist (TELEGRAM_ALLOWED_USERS / TELEGRAM_ALLOWED_CHATS / TELEGRAM_CHAT_ID)
- Fail-closed access control on all commands and callback queries
- Persistent pending posts stored in SQLite (survives bot restarts)
- Replay attack prevention on inline approval buttons
- No credential leakage in logs
- Resilient background health check server
"""

import asyncio
import io
import logging
import os
import sys
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from config.settings import settings
from src.content_extractor import ContentExtractor
from src.history_tracker import HistoryTracker
from src.hn_fetcher import HNFetcher
from src.linkedin_poster import LinkedInPoster
from src.post_generator import PostGenerator

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram_bot")


def is_authorized(update: Update) -> bool:
    """
    Check if the incoming user or chat ID is in the authorized allowlist.
    Fails closed if no allowed IDs are configured.
    """
    allowed_ids = settings.get_telegram_allowed_ids()
    if not allowed_ids:
        logger.warning("Access denied: No Telegram allowed IDs configured (fail-closed)")
        return False

    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None

    if (user_id and user_id in allowed_ids) or (chat_id and chat_id in allowed_ids):
        return True

    logger.warning("Access denied for unauthorized user_id=%s, chat_id=%s", user_id, chat_id)
    return False


async def check_command_auth(update: Update) -> bool:
    """Validate authorization for standard commands. Informs caller if unauthorized."""
    if not is_authorized(update):
        if update.message:
            await update.message.reply_text(
                "⛔ Unauthorized: You do not have permission to execute commands on this bot."
            )
        return False
    return True


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    if not await check_command_auth(update):
        return

    await update.message.reply_text(
        "🤖 HN-to-LinkedIn Bot (Security-Hardened)\n\n"
        "Commands:\n"
        "  /fetch — Fetch HN stories & generate 3 viral posts\n"
        "  /fetch N — Generate N posts (max 10)\n"
        "  /pending — List posts awaiting approval\n"
        "  /status — Check bot and credentials status\n"
        "  /history — Show posted stories count\n",
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command."""
    if not await check_command_auth(update):
        return

    tracker = HistoryTracker()
    pending = tracker.get_all_pending_posts(status="pending")
    allowed_count = len(settings.get_telegram_allowed_ids())

    await update.message.reply_text(
        "✅ Bot is running\n"
        f"📝 Pending posts in DB: {len(pending)}\n"
        f"👥 Authorized IDs configured: {allowed_count}\n"
        f"🔑 LinkedIn token: {'✅ Set' if settings.linkedin_access_token else '❌ Missing'}\n"
        f"👤 Person URN: {'✅ Set' if settings.linkedin_person_urn else '❌ Missing'}\n"
        f"💾 Database path: {tracker.db_path}",
    )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /history command."""
    if not await check_command_auth(update):
        return

    tracker = HistoryTracker()
    posted_ids = tracker.get_all_posted_ids()
    await update.message.reply_text(f"📊 Total stories posted to LinkedIn: {len(posted_ids)}")


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /pending command to review unapproved drafts stored in SQLite."""
    if not await check_command_auth(update):
        return

    tracker = HistoryTracker()
    pending = tracker.get_all_pending_posts(status="pending")

    if not pending:
        await update.message.reply_text("✨ No pending posts awaiting approval.")
        return

    await update.message.reply_text(f"📋 Found {len(pending)} pending posts in database. Presenting drafts:")

    for post in pending:
        post_id = post["post_id"]
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Post to LinkedIn", callback_data=f"approve:{post_id}"),
                InlineKeyboardButton("⏭️ Skip", callback_data=f"skip:{post_id}"),
            ]
        ])

        header = (
            f"🔗 {post['hn_title']}\n"
            f"⬆️ {post.get('hn_score', 0)} pts · 💬 {post.get('hn_comments', 0)} comments\n\n"
            f"━━━ LINKEDIN DRAFT ━━━\n\n"
            f"{post['linkedin_text']}\n\n"
            f"━━━ END ━━━"
        )

        if post.get("image_data"):
            try:
                photo = io.BytesIO(post["image_data"])
                photo.name = "preview.jpg"
                await update.message.reply_photo(photo=photo, caption="🖼️ Attached Image Preview:")
            except Exception:
                pass

        await update.message.reply_text(header, reply_markup=keyboard)


async def fetch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /fetch — run the full pipeline and persist posts for approval."""
    if not await check_command_auth(update):
        return

    count = 3
    if context.args:
        try:
            count = int(context.args[0])
            count = max(1, min(count, 10))
        except ValueError:
            pass

    await update.message.reply_text(f"🔄 Fetching top stories from Hacker News...\nWill generate {count} viral posts.")

    tracker = HistoryTracker()

    # Step 1: Fetch stories
    try:
        fetcher = HNFetcher(
            top_count=settings.hn_top_stories_count,
            random_rising_count=settings.hn_random_rising_count,
            min_score=settings.hn_min_score,
        )
        stories = fetcher.get_mixed_stories()
    except Exception as e:
        logger.error("Failed to fetch HN stories: %s", e)
        await update.message.reply_text(f"❌ Failed to fetch HN stories: {e}")
        return

    # Filter already posted stories
    posted_ids = tracker.get_all_posted_ids()
    stories = [s for s in stories if s["id"] not in posted_ids]

    if not stories:
        await update.message.reply_text("🤷 No new stories found. Try again later!")
        return

    await update.message.reply_text(f"📰 Found {len(stories)} new stories. Extracting content & images...")

    # Step 2: Extract content + OG images
    extractor = ContentExtractor(max_chars=settings.content_max_chars)
    stories_with_content = []
    for story in stories:
        url = story.get("url", "")
        if not url:
            continue
        result = extractor.extract_with_image(url)
        if result:
            story["content"] = result["text"]
            story["image_url"] = result.get("image_url")
            stories_with_content.append(story)

    if not stories_with_content:
        await update.message.reply_text("❌ Could not extract content from any stories.")
        return

    with_images = sum(1 for s in stories_with_content if s.get("image_url"))
    await update.message.reply_text(
        f"📄 Extracted {len(stories_with_content)} stories ({with_images} with images)\n"
        f"🤖 Generating {count} viral LinkedIn posts..."
    )

    # Step 3: Generate and persist posts in SQLite
    generator = PostGenerator()
    generated = 0

    for story in stories_with_content:
        if generated >= count:
            break

        post_text = generator.generate(
            article_text=story["content"],
            hn_title=story["title"],
            hn_url=story["url"],
            hn_score=story.get("score", 0),
            hn_comments=story.get("descendants", 0),
        )

        if not post_text:
            continue

        generated += 1
        post_id = f"post_{story['id']}_{int(asyncio.get_event_loop().time())}"

        # Download image if available
        image_data = None
        if story.get("image_url"):
            image_data = extractor.download_image(story["image_url"])

        # Persist to SQLite immediately (survives restarts)
        tracker.save_pending_post(
            post_id=post_id,
            hn_id=story["id"],
            title=story["title"],
            url=story["url"],
            score=story.get("score", 0),
            comments=story.get("descendants", 0),
            linkedin_text=post_text,
            image_url=story.get("image_url"),
            image_data=image_data,
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Post to LinkedIn", callback_data=f"approve:{post_id}"),
                InlineKeyboardButton("⏭️ Skip", callback_data=f"skip:{post_id}"),
            ]
        ])

        header = (
            f"📝 Post {generated}/{count}\n\n"
            f"🔗 {story['title']}\n"
            f"⬆️ {story.get('score', 0)} pts · 💬 {story.get('descendants', 0)} comments\n"
            f"{'🖼️ Image: YES' if image_data else '📝 Image: NO (text-only post)'}\n\n"
            f"━━━ LINKEDIN POST ━━━\n\n"
            f"{post_text}\n\n"
            f"━━━ END ━━━"
        )

        # Send preview image if present
        if image_data and story.get("image_url"):
            try:
                photo = io.BytesIO(image_data)
                photo.name = "preview.jpg"
                await update.message.reply_photo(photo=photo, caption="🖼️ Image that will be posted with this:")
            except Exception as e:
                logger.warning("Failed to send image preview: %s", e)

        # Send post text with approval markup
        if len(header) > 4096:
            chunks = [header[i:i + 4096] for i in range(0, len(header), 4096)]
            for i, chunk in enumerate(chunks):
                if i == len(chunks) - 1:
                    await update.message.reply_text(chunk, reply_markup=keyboard)
                else:
                    await update.message.reply_text(chunk)
        else:
            await update.message.reply_text(header, reply_markup=keyboard)

    if generated == 0:
        await update.message.reply_text("❌ AI failed to generate any posts. Try again shortly.")
    else:
        await update.message.reply_text(f"✅ {generated} posts persisted to database and ready for review ☝️")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button presses (approve/skip) with authorization and replay protection."""
    query = update.callback_query
    await query.answer()

    # 1. Verify authorization for callback query
    if not is_authorized(update):
        await query.answer("⛔ Unauthorized: You do not have permission to approve or skip posts.", show_alert=True)
        return

    data = query.data or ""
    if ":" not in data:
        await query.message.reply_text("⚠️ Invalid callback payload.")
        return

    action, post_id = data.split(":", 1)
    tracker = HistoryTracker()
    post = tracker.get_pending_post(post_id)

    if not post:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⚠️ Post record not found in database.")
        return

    # Check status for replay protection
    if post["status"] != "pending":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"⚠️ This post has already been processed (status: {post['status']}).")
        return

    if action == "skip":
        success = tracker.claim_and_mark_skipped(post_id)
        await query.edit_message_reply_markup(reply_markup=None)
        if success:
            await query.message.reply_text(f"⏭️ Skipped: {post['hn_title'][:50]}...")
        else:
            await query.message.reply_text("⚠️ Post was already processed by another action.")
        return

    if action == "approve":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("📤 Posting to LinkedIn...")

        poster = LinkedInPoster()
        post_urn = None

        if post.get("image_data"):
            post_urn = poster.create_image_post(
                text=post["linkedin_text"],
                image_data=post["image_data"],
            )
        else:
            post_urn = poster.create_text_post(
                text=post["linkedin_text"],
            )

        if post_urn:
            success = tracker.claim_and_mark_published(post_id, post_urn)
            if success:
                await query.message.reply_text(
                    f"✅ Successfully published to LinkedIn!\n"
                    f"📄 {post['hn_title'][:60]}\n"
                    f"🔗 {post_urn}"
                )
            else:
                await query.message.reply_text(
                    f"⚠️ Published to LinkedIn ({post_urn}), but status transition was already recorded."
                )
        else:
            tracker.mark_failed(post_id, "LinkedIn UGC API post creation failed")
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔄 Retry", callback_data=f"approve:{post_id}"),
                    InlineKeyboardButton("⏭️ Skip", callback_data=f"skip:{post_id}"),
                ]
            ])
            await query.message.reply_text(
                "❌ Failed to post to LinkedIn. Check your LinkedIn access token.\nYou can retry or skip:",
                reply_markup=keyboard,
            )


def _start_health_server():
    """Lightweight HTTP server on configured port for cloud keep-alive."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    port = int(os.environ.get("PORT", "7860"))
    try:
        server = HTTPServer(("0.0.0.0", port), Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        logger.info("Health server started on port %s", port)
    except Exception as e:
        logger.warning("Could not start health server on port %s: %s", port, e)


def main():
    """Start the Telegram bot with security validation."""
    bot_token = settings.telegram_bot_token.strip()
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set in environment or .env")
        sys.exit(1)

    allowed_ids = settings.get_telegram_allowed_ids()
    if not allowed_ids:
        logger.error(
            "Security lockdown (fail-closed): No authorized Telegram users or chats configured. "
            "Set TELEGRAM_ALLOWED_USERS, TELEGRAM_ALLOWED_CHATS, or TELEGRAM_CHAT_ID in .env."
        )
        sys.exit(1)

    logger.info("Starting HN-to-LinkedIn Telegram Bot with %d authorized ID(s)", len(allowed_ids))
    _start_health_server()

    from telegram.request import HTTPXRequest
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    app = Application.builder().token(bot_token).request(request).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("fetch", fetch_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("pending", pending_command))
    app.add_handler(CallbackQueryHandler(button_callback))

    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=5)


if __name__ == "__main__":
    main()
