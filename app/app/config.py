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
    # Посылки файлов от внешнего агента (services/exchange_files.py): пусто — data_dir/exchange_files.
    exchange_files_dir: Path | None = None
    exchange_files_reserve_gb: int = 20  # приём отклоняется, если после него на диске останется меньше
    exchange_bundle_max_gb: int = 64  # предел одной посылки
    exchange_auto_unpack: bool = True  # принятая посылка сама раскладывается в data/projects/<id>/config (фоном)

    web_host: str = "127.0.0.1"
    web_port: int = 1111
    # Публичный URL для ссылок и MCP (если портал доступен из интернета).
    web_public_url: str = ""
    # HTTPS / ACME (Caddy). Публичный :80 приходит в портал, не в Apache.
    skkkk_public_host: str = ""
    skkkk_https_port: int = 777
    skkkk_acme_http_port: int = 8088
    skkkk_acme_tls_port: int = 8443
    skkkk_acme_email: str = ""
    skkkk_acme_ca: str = ""
    skkkk_tls_cert: str = ""
    skkkk_tls_key: str = ""
    skkkk_tls_renew_hours: int = 48
    skkkk_tls_issue_timeout_sec: int = 120

    lmstudio_base_url: str = "http://127.0.0.1:1234/v1"
    lmstudio_model: str = "swift-qwen3.8-27b-uncensored-dynamic-mtp"
    lmstudio_api_key: str = "lm-studio"
    # Рассуждения локальной модели. enable_thinking управляет Jinja-переменной
    # enable_thinking в шаблоне Qwen, reasoning_effort — уровнем усилия
    # (none/minimal/low/medium/high/xhigh; см. _LMSTUDIO_REASONING_LEVELS).
    # По умолчанию мышление ВЫКЛЮЧЕНО: при ~2.4-3k reasoning-токенов на вызов
    # короткие вызовы (chat/entities/normalize/ASR, max_tokens 2-8k) вернули бы
    # пустой контент. Включается точечно там, где бюджет рассчитан: ТЗ-конвейер.
    lmstudio_enable_thinking: bool = False
    lmstudio_reasoning_effort: str = "medium"
    # Хранитель модели (services/llm_keeper.py, профиль tools/lmstudio/qwen-profile.json):
    # выгрузка после простоя без задач и чата; TTL LM Studio — страховка, если процессы SKKKK упали.
    lmstudio_idle_unload_sec: int = 300
    lmstudio_load_ttl_sec: int = 1800
    # Модель рядом с ASR на GPU. Замер 2026-10-05: на карте 24 ГБ не помещается (вынос в общую память, шаги встают),
    # поэтому по умолчанию false — модель выгружается на время GPU-шагов ASR (llm_keeper.asr_gpu_hold), чат ждёт.
    lmstudio_keep_during_asr: bool = False
    # Доля общего окна модели под полосу чата (промпт + ответ); остальное — фоновой полосе (services/llm_lanes.py).
    lmstudio_chat_token_budget: int = 22000

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
    # Диаризация lab: nemotron3 (до 8 спикеров, venvs/nemo-diar) или sortformer4 (откат, ONNX)
    diar_model: str = "nemotron3"
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
    jwt_expire_minutes: int = 43_200
    job_max_attempts: int = 3
    login_allowlist_path: Path = ROOT / "Login_up.txt"
    # Личный пароль после пароля роли: required (всегда) | off — только тесты и аварийный откат.
    personal_password: str = "required"
    # Пусто — `DATA_DIR/credentials.db`: изолированный экземпляр со своим DATA_DIR (QA, тесты) не трогает боевую базу.
    credentials_db_path: Path | None = None
    personal_password_min_len: int = 10
    # Компании для карточки пользователя (как Login_up.txt): «Название | Исполнитель|Заказчик».
    companies_path: Path = ROOT / "Companies.txt"
    # Логины, которым открыта панель пользователей (/admin/users): по строке на логин, как Login_up.txt. Нет файла — нет доступа.
    panel_users_path: Path = ROOT / "Panel_users.txt"
    # Как модель называет компанию-исполнителя в поле «исполнитель» задачи («компании X», через запятую): такой
    # исполнитель общий, при склейке задач его заменяет конкретный человек. Названия компаний в код не пишем.
    executor_aliases: str = ""

    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    # Шумодав голоса — DFN3 в браузере говорящего (на телефоне — шумоподавление браузера); 0 — везде шумоподавление браузера
    meeting_voice_dfn3: bool = True
    meeting_voice_loudness: bool = True
    # Рекордер голоса (tools/livekit/voice_cleaner.py): пишет дорожки людей для двойного слуха
    meeting_voice_server: bool = True
    livekit_rtc_internal: str = "ws://127.0.0.1:7880"

    # Пауза после lab-стенограммы: имена спикеров, затем knowledge
    voice_gate_before_asr: bool = True
    # strict — ждать, пока все голоса названы (как было); soft — знания строятся сразу, неизвестные голоса остаются
    # speaker_N и называются позже (R3 docs/plans/AUTO-REVIEW-PLAN.md); off — без паузы. voice_gate_before_asr=false → off.
    voice_gate_mode: str = "soft"

    # Автопроверка локальной моделью (docs/plans/AUTO-REVIEW-PLAN.md): off | shadow | live.
    # shadow — вердикты пишутся без последствий (для замера согласия с человеком), live — применяются.
    auto_review_mode: str = "shadow"

    # Платформа памяти решений (docs/plans/PLATFORM-FOUNDATION-PLAN.md): реестр людей, стороны по проекту, голос -> человек.
    # false — прежняя логика сторон; проект включает своей настройкой ``memory_platform`` (главнее общей из .env).
    memory_platform: bool = False
    # Память решений (docs/plans/DECISION-MEMORY-STAGES-6-8.md): off | shadow | live по умолчанию; проект включает своей настройкой
    # ``decision_memory``. shadow — строится и видна админу, чат её не использует; live — видна менеджерам и используется чатом.
    # auto_live: после построения проект сам переходит shadow -> live, когда автоматические проверки готовности пройдены.
    decision_memory_mode: str = "off"
    decision_memory_auto_live: bool = True
    auto_review_audit_every: int = 20  # каждое N-е авторешение дополнительно идёт человеку
    auto_review_batch: int = 5  # сущностей на один вызов LLM
    voice_auto_accept_min: float = 0.85
    voice_auto_accept_margin: float = 0.10

    @field_validator("personal_password", mode="before")
    @classmethod
    def _personal_password(cls, v: str | None) -> str:
        # Опечатка не должна молча выключить защиту: неизвестное значение — ошибка старта.
        mode = (v or "required").strip().lower()
        if mode == "optional":
            raise ValueError("PERSONAL_PASSWORD=optional больше нет: личный пароль обязателен (required)")
        if mode not in {"off", "required"}:
            raise ValueError("PERSONAL_PASSWORD: required | off (off — только тесты и аварийный откат)")
        return mode

    @field_validator("data_dir", "login_allowlist_path", "companies_path", "panel_users_path", mode="before")
    @classmethod
    def _path(cls, v: str | Path) -> Path:
        return Path(v)

    @field_validator("credentials_db_path", "exchange_files_dir", mode="before")
    @classmethod
    def _optional_path(cls, v: str | Path | None) -> Path | None:
        return Path(v) if v else None

    @property
    def credentials_db_file(self) -> Path:
        return self.credentials_db_path or self.data_dir / "credentials.db"


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
