from __future__ import annotations

from pathlib import Path

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    secret_key: str = Field(min_length=8)
    admin_password_hash: str = ""
    manager_password_hash: str = ""
    user_password_hash: str = ""

    database_url: str = f"sqlite:///{ROOT / 'data' / 'app.db'}"
    data_dir: Path = ROOT / "data"

    web_host: str = "127.0.0.1"
    web_port: int = 1111
    # Публичный URL для ссылок и MCP (если портал доступен из интернета).
    web_public_url: str = ""

    lmstudio_base_url: str = "http://127.0.0.1:1234/v1"
    lmstudio_model: str = "google/gemma-4-31b-qat"
    lmstudio_api_key: str = "lm-studio"

    llm_provider: str = "lmstudio"
    cursor_api_key: str = ""
    cursor_model: str = "composer-2.5"
    cursor_runtime: str = "local"
    cursor_cloud_repo: str = ""

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _llm_provider(cls, v: str | None) -> str:
        from app.app.llm_settings import normalize_llm_provider

        return normalize_llm_provider(v)

    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_embed_model: str = "bge-m3"
    ollama_num_gpu: int = 0

    whisper_url: str = "http://127.0.0.1:18101"
    gigaam_url: str = "http://127.0.0.1:18102"
    tone_url: str = "http://127.0.0.1:18103"
    gigaam_api_key: str = ""

    telegram_bot_token: str = ""

    max_upload_bytes: int = 2_147_483_648
    upload_chunk_size: int = 4_194_304
    max_audio_duration_sec: int = 14_400
    # Порог склейки сущностей в графе: срабатывает на почти дословных повторах.
    similarity_threshold: float = 0.82
    # Порог выдачи поиска и RAG-чата. Косинусная близость bge-m3 для осмысленного
    # совпадения короткого вопроса с абзацем лежит в районе 0.4–0.6, поэтому
    # порог графа здесь отсекал бы вообще всё.
    search_min_score: float = 0.35
    tz_rebuild_every_n: int = 3
    dry_run: bool = False
    jwt_expire_minutes: int = 720
    job_max_attempts: int = 3

    @field_validator("data_dir", mode="before")
    @classmethod
    def _path(cls, v: str | Path) -> Path:
        return Path(v)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def external_base_url(settings: Settings, request_base_url: str | None = None) -> str:
    """Базовый URL для внешних клиентов (MCP, ссылки). WEB_PUBLIC_URL приоритетнее request."""
    explicit = (settings.web_public_url or "").strip().rstrip("/")
    if explicit:
        return explicit
    if request_base_url:
        return str(request_base_url).rstrip("/")
    host = settings.web_host
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{settings.web_port}"
