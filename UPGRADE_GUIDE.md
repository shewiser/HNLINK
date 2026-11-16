# HN-Link v2.0.0 Upgrade & Migration Guide

This guide details how to upgrade existing HN-Link installations from v1.x to **HN-Link v2.0.0**.

---

## 📋 Overview of Changes

HN-Link v2.0.0 introduces critical security enhancements, database-level atomic idempotency, SQLite connection lifecycle management, and race condition prevention.

Upgrading preserves all existing posting history, but requires updating configuration files (`.env`) to support new security parameters.

---

## ⚠️ Breaking Changes & Upgrades Checklist

1. **Telegram Allowlist**: You must configure `TELEGRAM_ALLOWED_USERS` in your `.env`. If this variable is omitted or empty, all incoming Telegram messages and callbacks fail closed (rejected).
2. **Flask API Authentication & Required Identifiers**: The endpoints `/pipeline/fetch-and-generate` and `/pipeline/post-to-linkedin` require a Bearer token or `X-API-Key` header matching `HN_LINK_API_KEY` or `N8N_WEBHOOK_TOKEN`. Query parameter tokens (`?token=...`) are no longer permitted. Additionally, `POST /pipeline/post-to-linkedin` requires either a positive integer `hn_id` or an `idempotency_key`. Submissions without a valid identifier return `HTTP 400 Bad Request`.
3. **CLI Arguments**: The flags `--publish` and `--dry-run` are now mutually exclusive. Omitting both flags defaults to safe dry-run mode. Passing both will result in an argument error.
4. **API Idempotency Codes**: Replaying `POST /pipeline/post-to-linkedin` with the same `hn_id` or `idempotency_key` returns `HTTP 409 Conflict`. External timeouts return `HTTP 504 Gateway Timeout`.
5. **Persistent Credential Storage**: Rotated LinkedIn OAuth tokens are stored in `.credentials_store.json` (permissions `0600`). You can set `HN_LINK_ENCRYPTION_KEY` in `.env` to enable authenticated HMAC-SHA256 encryption at rest.

---

## 🛠 Step-by-Step Upgrade Procedure

### Step 1: Backup Your Existing Database

Before updating code, create a safety copy of your SQLite database:

```bash
# If your database is in the default location:
cp post_history.db post_history.db.v1.backup

# If your database is in a parent directory (legacy path):
cp ../post_history.db ../post_history.db.v1.backup
```

### Step 2: Pull Latest Code

```bash
git fetch origin
git checkout security-hardening # Or v2.0.0 release tag once published
```

### Step 3: Update Dependencies

Activate your virtual environment and install dependencies:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### Step 4: Update Your `.env` Configuration

Review `.env.example` and update your existing `.env` file with the required security variables:

```bash
# 1. Telegram Allowlist (obtain your numeric ID from @userinfobot)
TELEGRAM_ALLOWED_USERS=123456789

# 2. Flask API Authentication (generate a random 32-character token)
HN_LINK_API_KEY=your_generated_random_secret_token_here
N8N_WEBHOOK_TOKEN=your_generated_random_secret_token_here

# 3. Safe Network Defaults
FLASK_HOST=127.0.0.1
FLASK_PORT=5000
FLASK_DEBUG=false
```

### Step 5: Automatic Database Migration

HN-Link v2.0.0 includes automatic, non-destructive schema migrations. The first time any v2.0.0 component initializes, `HistoryTracker._init_db()` will automatically:
1. Verify the presence of `posted_stories`.
2. Add the `status` and `error_message` columns if upgrading from a v1.x schema.
3. Create the `pending_posts` table and its associated indexes for persistent approvals.
4. Preserve all existing historical records.

To manually verify database integrity and migration status:

```bash
python -c "from src.history_tracker import HistoryTracker; t = HistoryTracker(); print('Integrity:', t.check_integrity())"
```

Expected output:
```text
Integrity: (True, 'ok')
```

---

## 🔄 Handling Ambiguous Publishing States

In v2.0.0, if an external network timeout occurs during LinkedIn publishing, the post status is set to:
`reconciliation_needed`

This state prevents automatic duplicate publication. To inspect or reconcile posts:
1. Check your LinkedIn personal profile to verify whether the post appeared.
2. If the post succeeded on LinkedIn, no action is needed.
3. If the post failed to publish, you can clear or reset the status via Python or SQLite CLI:
   ```sql
   UPDATE posted_stories SET status = 'failed' WHERE hn_id = <story_id>;
   ```

---

## 🔍 Rollback Procedure

If you need to revert to v1.x:

1. Restore your backed-up database:
   ```bash
   cp post_history.db.v1.backup post_history.db
   ```
2. Checkout the v1.x commit:
   ```bash
   git checkout main
   ```
3. Restart your service.
