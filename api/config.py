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

    class Config:
        env_file = ".env"


# ── Module-level singleton ─────────────────────────────────────────────

_settings: Settings = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
