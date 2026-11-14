# Changelog

All notable changes to the HN-Link project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.0.0] - 2026-11-15 (Release Candidate)

HN-Link v2.0.0 is a major version release providing comprehensive application security hardening, database-level atomic idempotency, connection lifecycle management, and race condition remediation while preserving the core Hacker News to LinkedIn curation and publishing workflow.

### 🚨 Breaking Changes
- **CLI Mode Precedence**: `--dry-run` and `--publish` are now mutually exclusive flags enforced via `argparse.MutuallyExclusiveGroup`. Specifying both simultaneously raises a syntax error (exit code 2). `--dry-run` remains the safe default.
- **Flask API Authentication**: All publishing and pipeline endpoints (`/pipeline/fetch-and-generate`, `/pipeline/post-to-linkedin`) require valid bearer authentication (`Authorization: Bearer <token>` or `X-API-Key: <token>`). Tokens supplied in query parameters are strictly rejected with HTTP 400 Bad Request.
- **Telegram Authorization Allowlist**: In Telegram bot mode, only user IDs configured in `TELEGRAM_ALLOWED_USERS` or chat IDs in `TELEGRAM_ALLOWED_CHATS` can invoke commands or click inline buttons. Fails closed if empty.
- **API Idempotency & Conflict Semantics**: Repeated or concurrent requests to `POST /pipeline/post-to-linkedin` for the same `hn_id` now return HTTP 409 Conflict. Requests that experience external timeouts return HTTP 504 Gateway Timeout and transition to `reconciliation_needed` state.

### 🛡️ Security Improvements
- **Telegram TOCTOU Race Condition Elimination**: Introduced database-level atomic claim transitions (`pending -> publishing -> published`) prior to invoking external LinkedIn UGC endpoints. Concurrently tapped "Approve" buttons or multi-administrator races are rejected immediately, preventing duplicate publications to the live LinkedIn feed.
- **Single Callback Query Answering**: Callback queries answer exactly once after authorization validation, eliminating unhandled `BadRequest` exceptions on unauthorized attempts.
- **Constant-Time API Token Comparison**: Implemented constant-time authentication via `hmac.compare_digest` to prevent timing side-channel attacks on API tokens.
- **Localhost Binding & Debug Safe Defaults**: Default Flask configuration binds strictly to `127.0.0.1` with `debug=False`, preventing unintentional public exposure.
- **Non-Interactive TTY Enforcement**: CLI publishing mode verifies `sys.stdin.isatty()` and aborts with a security halt if invoked non-interactively without human post-by-post confirmation.

### ⚡ Reliability & Architecture
- **SQLite Connection Management**: Refactored `HistoryTracker` with an explicit context manager ensuring all connections execute `conn.close()` upon exit, completely preventing SQLite connection and file handle leaks.
- **Persistent Pending Approvals**: Drafts awaiting Telegram approval are persisted in SQLite (`pending_posts`), surviving application restarts.
- **Ambiguous Network Reconciliation**: When a network timeout occurs while contacting LinkedIn, posts transition to `reconciliation_needed` rather than retrying blindly, protecting users from duplicate posts.
- **Targeted OAuth Token Refresh**: LinkedIn token refresh is restricted strictly to confirmed HTTP 401 Unauthorized responses, preventing rate-limiting on token endpoints during 400, 403, 429, or 5xx responses.
- **Refresh Token Rotation**: Automatically captures and stores rotated refresh tokens returned by LinkedIn OAuth endpoints.
- **Non-Destructive Database Migrations**: `HistoryTracker._init_db()` automatically migrates existing databases, preserving all historical publication records.

### 🧪 Automated Testing
- Added comprehensive automated test suite with 40 tests covering API security, CLI safety, SQLite transactions and WAL mode, LinkedIn token lifecycles, and Telegram concurrency locks.
- Tested and verified on Python 3.10, 3.12, and 3.14.

---

## [1.0.0] - Legacy Release

- Initial public release of Hacker News to LinkedIn automation pipeline.
- Trafilatura content extraction and BeautifulSoup fallback.
- OpenRouter AI prompt generation.
- LinkedIn UGC text and image publishing.
- Basic Telegram bot interface for human review.
- SQLite deduplication history.
