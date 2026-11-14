import os
from dataclasses import dataclass, field
from typing import Set


@dataclass
class Settings:
    # OpenRouter
    openrouter_api_key: str = field(
        default_factory=lambda: os.environ.get("OPENROUTER_API_KEY", "")
    )
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "deepseek/deepseek-v4-flash:free"

    # LinkedIn
    linkedin_access_token: str = field(
        default_factory=lambda: os.environ.get("LINKEDIN_ACCESS_TOKEN", "")
    )
    linkedin_person_urn: str = field(
        default_factory=lambda: os.environ.get("LINKEDIN_PERSON_URN", "")
    )
    linkedin_client_id: str = field(
        default_factory=lambda: os.environ.get("LINKEDIN_CLIENT_ID", "")
    )
    linkedin_client_secret: str = field(
        default_factory=lambda: os.environ.get("LINKEDIN_CLIENT_SECRET", "")
    )
    linkedin_refresh_token: str = field(
        default_factory=lambda: os.environ.get("LINKEDIN_REFRESH_TOKEN", "")
    )

    # API & Flask Server
    api_auth_token: str = field(
        default_factory=lambda: os.environ.get("HN_LINK_API_KEY") or os.environ.get("N8N_WEBHOOK_TOKEN", "")
    )
    flask_host: str = field(
        default_factory=lambda: os.environ.get("FLASK_HOST", "127.0.0.1")
    )
    flask_port: int = field(
        default_factory=lambda: int(os.environ.get("FLASK_PORT", os.environ.get("PORT", "5000")))
    )
    flask_debug: bool = field(
        default_factory=lambda: os.environ.get("FLASK_DEBUG", "false").lower() in ("true", "1", "yes")
    )

    # n8n
    n8n_base_url: str = field(
        default_factory=lambda: os.environ.get("N8N_BASE_URL", "http://localhost:5678")
    )
    n8n_webhook_token: str = field(
        default_factory=lambda: os.environ.get("N8N_WEBHOOK_TOKEN", "")
    )

    # WhatsApp
    whatsapp_recipient: str = field(
        default_factory=lambda: os.environ.get("WHATSAPP_RECIPIENT_NUMBER", "")
    )

    # Telegram Authorization
    telegram_bot_token: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", "")
    )
    telegram_chat_id: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID", "")
    )
    telegram_allowed_users: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    )
    telegram_allowed_chats: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_ALLOWED_CHATS", "")
    )

    # Database
    db_path: str = field(
        default_factory=lambda: os.environ.get("HN_LINK_DB_PATH", "")
    )

    # HN Bot
    hn_top_stories_count: int = field(
        default_factory=lambda: int(os.environ.get("HN_TOP_STORIES_COUNT", "15"))
    )
    hn_random_rising_count: int = field(
        default_factory=lambda: int(os.environ.get("HN_RANDOM_RISING_COUNT", "5"))
    )
    hn_min_score: int = field(
        default_factory=lambda: int(os.environ.get("HN_MIN_SCORE", "50"))
    )
    content_max_chars: int = field(
        default_factory=lambda: int(os.environ.get("CONTENT_MAX_CHARS", "8000"))
    )
    posts_to_generate: int = field(
        default_factory=lambda: int(os.environ.get("POSTS_TO_GENERATE", "3"))
    )

    def get_telegram_allowed_ids(self) -> Set[int]:
        """
        Parse and return the set of authorized Telegram user/chat IDs.
        Checks TELEGRAM_ALLOWED_USERS, TELEGRAM_ALLOWED_CHATS, and falls back to TELEGRAM_CHAT_ID.
        """
        allowed: Set[int] = set()
        for source in [self.telegram_allowed_users, self.telegram_allowed_chats, self.telegram_chat_id]:
            if not source:
                continue
            for part in source.replace(";", ",").split(","):
                part = part.strip()
                if part:
                    try:
                        allowed.add(int(part))
                    except ValueError:
                        pass
        return allowed


settings = Settings()
