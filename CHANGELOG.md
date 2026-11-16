# Changelog

All notable changes to the HN-Link project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.0.0] - 2026-11-16 (Release Candidate 2)

HN-Link v2.0.0 RC2 resolves all outstanding findings from the independent release review, introducing persistent credential storage, mandatory API publication identifiers, and database input bounds.

### 🚨 Breaking Changes & API Hardening
- **Mandatory Publication Identifiers**: `POST /pipeline/post-to-linkedin` now strictly requires either a valid positive integer `hn_id` (for Hacker News stories) or an explicit `idempotency_key` (via JSON payload or `Idempotency-Key` header). Omitting both or providing non-positive/malformed IDs returns `HTTP 400 Bad Request`. No publishing path can bypass deduplication.
- **Persistent Idempotency Claims**: Added persistent `idempotency_claims` table in SQLite with a unique database constraint. Concurrent or duplicate requests for any story or key return `HTTP 409 Conflict`.
- **CLI Mode Precedence**: `--dry-run` and `--publish` are mutually exclusive flags enforced via `argparse.MutuallyExclusiveGroup`. Specifying both simultaneously raises a syntax error (exit code 2). `--dry-run` remains the safe default.
- **Flask API Authentication**: All publishing and pipeline endpoints require valid bearer authentication (`Authorization: Bearer <token>` or `X-API-Key: <token>`). Tokens supplied in query parameters are strictly rejected with HTTP 400 Bad Request.
- **Telegram Authorization Allowlist**: In Telegram bot mode, only user IDs configured in `TELEGRAM_ALLOWED_USERS` or chat IDs in `TELEGRAM_ALLOWED_CHATS` can invoke commands or click inline buttons. Fails closed if empty.

### 🛡️ Security Improvements
- **Persistent Secure Credential Store (Fix M-2)**: Implemented `SecureCredentialStore` to persist refreshed and rotated OAuth tokens across application restarts. Restricts file permissions to owner-only (`0600`), supports optional authenticated HMAC-SHA256 encryption-at-rest (`HN_LINK_ENCRYPTION_KEY`), and fails closed if credentials cannot be safely persisted. Zero credentials stored in plaintext SQLite.
- **Telegram TOCTOU Race Condition Elimination**: Database-level atomic claim transitions (`pending -> publishing -> published`) prior to invoking external LinkedIn UGC endpoints. Concurrently tapped "Approve" buttons or multi-administrator races are rejected immediately.
- **Database Input Bounds (Fix L-1)**: Enforced truncation limits on externally sourced metadata (`MAX_TITLE_CHARS = 500`, `MAX_URL_CHARS = 2048`, `MAX_POST_TEXT_CHARS = 25000`, `MAX_ERROR_CHARS = 2000`) preventing database bloat or malformed payload injection.
- **Constant-Time API Token Comparison**: Implemented constant-time authentication via `hmac.compare_digest` to prevent timing side-channel attacks on API tokens.
- **Localhost Binding & Debug Safe Defaults**: Default Flask configuration binds strictly to `127.0.0.1` with `debug=False`.
- **Non-Interactive TTY Enforcement**: CLI publishing mode verifies `sys.stdin.isatty()` and aborts with a security halt if invoked non-interactively without human confirmation.

### ⚡ Reliability & Architecture
- **SQLite Connection Management**: Context manager ensuring all connections execute `conn.close()` upon exit, completely preventing SQLite connection and file handle leaks.
- **Persistent Pending Approvals**: Drafts awaiting Telegram approval are persisted in SQLite (`pending_posts`), surviving application restarts.
- **Ambiguous Network Reconciliation**: When a network timeout occurs while contacting LinkedIn, posts transition to `reconciliation_needed` rather than retrying blindly.
- **Targeted OAuth Token Refresh**: LinkedIn token refresh is restricted strictly to confirmed HTTP 401 Unauthorized responses.
- **Non-Destructive Database Migrations**: `HistoryTracker._init_db()` automatically migrates existing databases, creating `idempotency_claims` and preserving all historical publication records.

### 🧪 Automated Testing
- Expanded automated test suite from 40 to **49 tests** covering missing/invalid `hn_id`, duplicate idempotency keys, generic non-HN publishing, credential persistence across restarts, credential persistence failure, and input bounds truncation.
- Verified on Python 3.12 (100% test pass rate).

---

## [1.0.0] - Legacy Release

- Initial public release of Hacker News to LinkedIn automation pipeline.
- Trafilatura content extraction and BeautifulSoup fallback.
- OpenRouter AI prompt generation.
- LinkedIn UGC text and image publishing.
- Basic Telegram bot interface for human review.
- SQLite deduplication history.
