"""
Security-hardened Flask API server that bridges n8n and the Python pipeline.

Endpoints:
  GET  /health                      - Public health check
  POST /pipeline/fetch-and-generate - Run full pipeline, return generated posts (Authenticated)
  POST /pipeline/post-to-linkedin   - Post approved content to LinkedIn (Authenticated)
"""

import functools
import hmac
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from flask import Flask, jsonify, request

from config.settings import settings
from src.content_extractor import ContentExtractor
from src.history_tracker import HistoryTracker
from src.hn_fetcher import HNFetcher
from src.linkedin_poster import LinkedInPoster
from src.post_generator import PostGenerator

app = Flask(__name__)


def require_api_auth(f):
    """
    Decorator that enforces token authentication.
    - Rejects tokens passed in query parameters (fail closed).
    - Requires Authorization: Bearer <token>, X-API-Key: <token>, or X-Webhook-Token: <token>.
    - Validates token using constant-time comparison (hmac.compare_digest).
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        # 1. Strictly forbid credentials in query parameters to prevent leakages in URLs/logs
        sensitive_params = {"token", "api_key", "webhook_token", "key", "auth", "secret"}
        if any(param in request.args for param in sensitive_params):
            return jsonify({
                "error": "Bad Request: Authentication tokens must not be passed in URL query parameters. Use the Authorization header or X-API-Key."
            }), 400

        # 2. Check that the server has an authentication secret configured (fail-closed)
        expected_token = (settings.api_auth_token or settings.n8n_webhook_token or "").strip()
        if not expected_token:
            return jsonify({
                "error": "Server Configuration Error: API authentication token is unconfigured (fail-closed). Set HN_LINK_API_KEY or N8N_WEBHOOK_TOKEN in .env."
            }), 503

        # 3. Extract token from supported headers
        auth_header = request.headers.get("Authorization", "").strip()
        provided_token = ""
        if auth_header.startswith("Bearer "):
            provided_token = auth_header[7:].strip()
        elif not auth_header:
            provided_token = (
                request.headers.get("X-API-Key", "").strip()
                or request.headers.get("X-Webhook-Token", "").strip()
            )

        if not provided_token:
            return jsonify({"error": "Unauthorized: Missing authentication token"}), 401

        # 4. Constant-time comparison to prevent timing attacks
        if not hmac.compare_digest(provided_token.encode("utf-8"), expected_token.encode("utf-8")):
            return jsonify({"error": "Unauthorized: Invalid authentication token"}), 401

        return f(*args, **kwargs)
    return decorated


@app.get("/health")
def health():
    """Public health check endpoint."""
    return jsonify({"status": "ok"})


@app.post("/pipeline/fetch-and-generate")
@require_api_auth
def fetch_and_generate():
    """Run the full pipeline: fetch HN -> extract -> generate posts."""
    data = request.get_json(silent=True) or {}
    count = data.get("count", settings.posts_to_generate)

    tracker = HistoryTracker()

    # Step 1: Fetch stories
    fetcher = HNFetcher(
        top_count=settings.hn_top_stories_count,
        random_rising_count=settings.hn_random_rising_count,
        min_score=settings.hn_min_score,
    )
    stories = fetcher.get_mixed_stories()

    # Filter out already-posted
    posted_ids = tracker.get_all_posted_ids()
    stories = [s for s in stories if s["id"] not in posted_ids]

    if not stories:
        return jsonify({"posts": [], "message": "No new stories found"})

    # Step 2: Extract content
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

    # Step 3: Generate LinkedIn posts
    generator = PostGenerator()
    generated_posts = []
    for story in stories_with_content:
        if len(generated_posts) >= count:
            break
        post_text = generator.generate(
            article_text=story["content"],
            hn_title=story["title"],
            hn_url=story["url"],
            hn_score=story.get("score", 0),
            hn_comments=story.get("descendants", 0),
        )
        if post_text:
            generated_posts.append({
                "hn_id": story["id"],
                "hn_title": story["title"],
                "hn_url": story["url"],
                "hn_score": story.get("score", 0),
                "hn_comments": story.get("descendants", 0),
                "article_summary": story["content"][:300],
                "linkedin_text": post_text,
            })

    return jsonify({
        "posts": generated_posts,
        "count": len(generated_posts),
    })


@app.post("/pipeline/post-to-linkedin")
@require_api_auth
def post_to_linkedin():
    """Post approved content to LinkedIn."""
    data = request.get_json(silent=True)
    if not data or "text" not in data:
        return jsonify({"error": "Missing 'text' field"}), 400

    text = data["text"]
    article_url = data.get("article_url", "")
    article_title = data.get("article_title", "")
    hn_id = data.get("hn_id", 0)
    hn_title = data.get("hn_title", "")

    poster = LinkedInPoster()

    if article_url:
        post_urn = poster.create_article_post(text, article_url, article_title)
    else:
        post_urn = poster.create_text_post(text)

    if post_urn:
        tracker = HistoryTracker()
        tracker.mark_posted(hn_id, hn_title, article_url, post_urn)
        return jsonify({"status": "posted", "post_urn": post_urn})

    return jsonify({"error": "Failed to post to LinkedIn"}), 500


if __name__ == "__main__":
    app.run(
        host=settings.flask_host,
        port=settings.flask_port,
        debug=settings.flask_debug,
    )
