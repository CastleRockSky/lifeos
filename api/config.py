from pydantic_settings import BaseSettings
import logging

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    # Database (asyncpg format)
    database_url: str = "postgresql://lifeos:lifeos@postgres:5432/lifeos"

    # Qdrant
    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection: str = "documents"

    # Anthropic (Phase 2)
    anthropic_api_key: str = ""

    # Storage
    upload_dir: str = "/data/documents"
    # Backup tarballs, surfaced read-only on the Settings page. Mounted from
    # ${DATA_PATH}/backups by docker-compose; written by scripts/backup.sh.
    backup_dir: str = "/data/backups"

    # Security
    secret_key: str = "change-me"
    allowed_origins: str = "*"

    # Embedding model (local, no API needed)
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    # Chunking
    chunk_size: int = 1000
    chunk_overlap: int = 200

    # Inbox watcher
    inbox_enabled: bool = True
    inbox_dir: str = "/data/documents/inbox"
    inbox_poll_interval: int = 10
    inbox_stability_seconds: int = 5

    # Email forwarding ingestion (Phase 3)
    # Disabled until IMAP credentials are provided.
    imap_enabled: bool = False
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"
    imap_poll_interval: int = 120          # seconds — spec calls for ~2 min
    imap_processed_label: str = "LifeOS/Processed"  # Gmail label or IMAP folder for processed mail
    imap_failed_label: str = "LifeOS/Failed"
    imap_use_ssl: bool = True
    imap_max_message_size: int = 50 * 1024 * 1024  # 50 MB cap on raw email size
    imap_max_retries: int = 3

    # Google Calendar sync (Phase 10)
    # Enable once google_credentials_path holds OAuth tokens (from
    # scripts/bootstrap_google_calendar.py).
    google_calendar_enabled: bool = False
    google_calendar_id: str = "primary"  # or a specific calendar ID for "LifeOS"
    google_credentials_path: str = "/data/auth/google-calendar-tokens.json"
    google_oauth_client_path: str = "/data/auth/google-oauth-client.json"
    # Comma-separated; empty means all
    google_calendar_domains: str = ""
    google_calendar_event_prefix: str = "[LifeOS] "
    google_calendar_link_base: str = ""  # e.g. https://lifeos.example.com

    # Hardening (Phase 12)
    max_upload_bytes: int = 100 * 1024 * 1024  # 100 MB
    allowed_upload_mime: str = (
        "application/pdf,image/jpeg,image/png,image/tiff,image/heic,image/webp,"
        "image/gif,image/bmp,text/plain,text/csv,application/json,"
        "application/msword,"
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
        "application/vnd.ms-excel,"
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    qa_rate_limit_per_minute: int = 10  # per-user; 0 disables

    class Config:
        env_file = ".env"


# ── Module-level singleton ─────────────────────────────────────────────

_settings: Settings = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
