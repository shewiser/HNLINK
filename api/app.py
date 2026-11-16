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
from src.linkedin_poster import LinkedInPoster, LinkedInTimeoutError
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
    return jsonify({"status": "ok", "version": "2.0.0"})


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
    """Post approved content to LinkedIn with atomic idempotency protection."""
    data = request.get_json(silent=True)
    if not data or "text" not in data or not str(data["text"]).strip():
        return jsonify({"error": "Missing or empty 'text' field"}), 400

    text = str(data["text"]).strip()
    article_url = data.get("article_url", "")
    article_title = data.get("article_title", "")
    hn_title = data.get("hn_title", "")

    # 1. Validate incoming publication identifier (Fix M-1)
    raw_hn_id = data.get("hn_id")
    idempotency_key = (
        request.headers.get("Idempotency-Key")
        or request.headers.get("X-Idempotency-Key")
        or data.get("idempotency_key")
    )

    hn_id = None
    if raw_hn_id is not None:
        try:
            hn_id = int(raw_hn_id)
            if hn_id <= 0:
                return jsonify({"error": "Invalid 'hn_id': Must be a positive integer."}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid 'hn_id': Must be a positive integer."}), 400

    # If neither hn_id nor idempotency_key is provided, fail closed
    if hn_id is None and not idempotency_key:
        return jsonify({
            "error": "Missing publication identifier: Must provide a positive 'hn_id' or an 'idempotency_key' (via body or Idempotency-Key header)."
        }), 400

    if idempotency_key:
        idempotency_key = str(idempotency_key).strip()
        if not idempotency_key or len(idempotency_key) > 255:
            return jsonify({"error": "Invalid 'idempotency_key': Must be a non-empty string under 256 characters."}), 400
    else:
        idempotency_key = f"hn:{hn_id}"

    tracker = HistoryTracker()

    # 2. Atomically claim idempotency key before calling LinkedIn
    claimed, reason = tracker.claim_idempotency_key(
        idempotency_key=idempotency_key,
        hn_id=hn_id,
        title=hn_title or article_title,
        url=article_url,
    )
    if not claimed:
        return jsonify({
            "error": f"Conflict: Post identifier '{idempotency_key}' has already been published or is currently being processed.",
            "status": reason,
            "idempotency_key": idempotency_key,
            "hn_id": hn_id,
        }), 409

    poster = LinkedInPoster()
    post_urn = None

    try:
        if article_url:
            post_urn = poster.create_article_post(text, article_url, article_title)
        else:
            post_urn = poster.create_text_post(text)
    except LinkedInTimeoutError as e:
        tracker.mark_claim_uncertain(idempotency_key, f"LinkedIn request timed out: {e}", hn_id=hn_id)
        return jsonify({
            "error": "Gateway Timeout: LinkedIn posting request timed out. Marked for reconciliation to prevent duplicate publishing.",
            "status": "reconciliation_needed",
            "idempotency_key": idempotency_key,
            "hn_id": hn_id,
        }), 504
    except Exception as e:
        tracker.mark_claim_failed(idempotency_key, f"LinkedIn posting failed: {e}", hn_id=hn_id)
        return jsonify({
            "error": f"Publishing failed: {e}",
            "idempotency_key": idempotency_key,
            "hn_id": hn_id,
        }), 500

    if post_urn:
        tracker.mark_claim_published(
            idempotency_key=idempotency_key,
            linkedin_post_urn=post_urn,
            hn_id=hn_id,
            title=hn_title or article_title,
            url=article_url,
        )
        return jsonify({
            "status": "posted",
            "post_urn": post_urn,
            "idempotency_key": idempotency_key,
            "hn_id": hn_id,
        })

    # Definite non-timeout failure
    tracker.mark_claim_failed(idempotency_key, "LinkedIn UGC API post creation failed", hn_id=hn_id)
    return jsonify({
        "error": "Failed to post to LinkedIn",
        "idempotency_key": idempotency_key,
        "hn_id": hn_id,
    }), 500


if __name__ == "__main__":
    app.run(
        host=settings.flask_host,
        port=settings.flask_port,
        debug=settings.flask_debug,
    )
