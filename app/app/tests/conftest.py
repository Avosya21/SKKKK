from __future__ import annotations



import os

os.environ.setdefault("PRIMARY_ANALITICS_STRICT_DB_HOLD", "1")  # вызов LLM при открытой транзакции БД в тестах — ошибка (держит соединение пула)
os.environ.setdefault("PRIMARY_ANALITICS_DISABLE_LLM_SELFCHECK", "1")  # самопроверка LLM при старте приложения будила бы настоящий LM Studio
os.environ.setdefault("PRIMARY_ANALITICS_DISABLE_LLM_KEEPER", "1")  # хранитель модели не вызывает lms: иначе тест загрузил бы 18 ГБ в GPU
os.environ.setdefault("PRIMARY_ANALITICS_STRICT_LLM_LANE", "1")  # фоновый вызов модели из процесса API в тестах — ошибка (llm_lanes)
os.environ.setdefault("PRIMARY_ANALITICS_DISABLE_EDGE", "1")  # тесты не запускают настоящие рекордеры записи (см. edge_runtime.edge_disabled)

from collections.abc import Generator

from pathlib import Path



import pytest

from fastapi.testclient import TestClient

from sqlalchemy import create_engine

from sqlalchemy.orm import sessionmaker



# Совместимость со старыми тестами, которые ещё импортируют имена (пароли ролей больше не используются).
ADMIN_PASS = "admin-pass"
MANAGER_PASS = "manager-pass"
USER_PASS = "user-pass"

os.environ.setdefault("SECRET_KEY", "test-secret-key-32chars!!")
os.environ["DRY_RUN"] = "1"
# Личный пароль по умолчанию off в тестах; required включают тесты личного пароля / карточки.
os.environ["PERSONAL_PASSWORD"] = "off"
os.environ["JWT_EXPIRE_MINUTES"] = "480"
# Коды входа в тестах — в лог/peek_dev_code, без реального B24/SMTP.
os.environ["B24_AUTH"] = "on"
os.environ["B24_AUTH_DEV"] = "1"
os.environ["EMAIL_OTP"] = "on"
os.environ["EMAIL_OTP_DEV"] = "1"
# В тестах не упираться в лимит кодов B24/email (кроме явных тестов лимитера).
os.environ["LOGIN_CODE_RATE_LOGIN_PER_HOUR"] = "1000"
os.environ["LOGIN_CODE_RATE_IP_PER_HOUR"] = "1000"





@pytest.fixture(autouse=True)

def _reset_login_limiter():

    from app.app.services.rate_limit import LOGIN_LIMITER

    LOGIN_LIMITER.reset()

    yield


@pytest.fixture(autouse=True)
def _livekit_offline(monkeypatch):
    """Тесты не ходят в живой LiveKit: настройки берутся из боевого .env, и завершение встречи в тесте закрывало бы
    комнату на настоящем сервере (``meetings.close_livekit_room``). Тесты самой функции подставляют свой модуль livekit."""
    try:
        from livekit import api
    except ImportError:
        yield
        return

    class _Offline:
        def __init__(self, *_a, **_kw):
            raise RuntimeError("tests: LiveKit offline")

    monkeypatch.setattr(api, "LiveKitAPI", _Offline)
    yield



@pytest.fixture(autouse=True)

def _forbid_real_db(monkeypatch):

    """Тест без изоляции БД не должен молча писать в боевую data/app.db (так в неё попали 9 проектов «Batch»).

    SessionLocal по умолчанию бросает ошибку; фикстуры client/db_engine подменяют его тестовым.
    """

    import app.app.db as db_mod

    def _blocked(*_a, **_k):

        raise RuntimeError("Тест обратился к боевой БД: используйте фикстуру client или db_engine")

    monkeypatch.setattr(db_mod, "SessionLocal", _blocked)





@pytest.fixture(autouse=True)
def _isolated_credentials_db(tmp_path, monkeypatch):
    """База личных паролей в тестах — временный файл, боевая data/credentials.db недоступна."""
    from app.app import credentials_db
    from app.app.services import personal_password

    monkeypatch.setattr(credentials_db, "PATH_OVERRIDE", tmp_path / "credentials.db")
    personal_password.clear_cache()
    yield
    personal_password.clear_cache()
    credentials_db.dispose_all()


@pytest.fixture(autouse=True)

def _import_models():

    import app.app.models  # noqa: F401





@pytest.fixture()

def tmp_data(tmp_path: Path, monkeypatch) -> Path:

    data = tmp_path / "data"

    data.mkdir()

    monkeypatch.setenv("DATA_DIR", str(data))

    monkeypatch.setenv("DATABASE_URL", "sqlite://")

    allow = tmp_path / "Login_up.txt"
    allow.write_text(
        "\n".join(
            [
                "Admin",
                "Manager",
                "User",
                "dup_login_keep",
                "Bootstrap admin",
                "Bootstrap manager",
                "Bootstrap user",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LOGIN_ALLOWLIST_PATH", str(allow))
    roles = tmp_path / "Login_roles.txt"
    roles.write_text(
        "\n".join(
            [
                "Admin | admin",
                "Manager | manager",
                "User | user",
                "Bootstrap admin | admin",
                "Bootstrap manager | manager",
                "Bootstrap user | user",
                "dup_login_keep | manager",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LOGIN_ROLES_PATH", str(roles))
    companies = tmp_path / "Companies.txt"
    companies.write_text("ACME | Исполнитель\nЗаказчик Тест | Заказчик\n", encoding="utf-8")
    monkeypatch.setenv("COMPANIES_PATH", str(companies))

    return data





@pytest.fixture()

def db_engine(tmp_data: Path, monkeypatch):

    from app.app.config import get_settings



    get_settings.cache_clear()

    db_path = tmp_data / "test.db"

    engine = create_engine(

        f"sqlite:///{db_path.as_posix()}",

        connect_args={"check_same_thread": False},

    )

    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    from app.app.db import Base

    import app.app.models  # noqa: F401



    Base.metadata.drop_all(bind=engine)

    Base.metadata.create_all(bind=engine)

    from app.app.db import ensure_audit_append_only

    with engine.connect() as conn:
        ensure_audit_append_only(conn)
        conn.commit()

    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)



    def _get_db():

        db = TestingSession()

        try:

            yield db

        finally:

            db.close()



    import app.app.main as main_mod

    import app.app.db as db_mod

    import app.app.workers.gpu_worker as gw_mod



    monkeypatch.setattr(db_mod, "engine", engine)

    monkeypatch.setattr(db_mod, "SessionLocal", TestingSession)

    monkeypatch.setattr(gw_mod, "SessionLocal", TestingSession)

    main_mod.app.dependency_overrides[db_mod.get_db] = _get_db

    yield engine

    main_mod.app.dependency_overrides.clear()

    get_settings.cache_clear()





@pytest.fixture()

def client(db_engine) -> Generator[TestClient, None, None]:

    from app.app.main import app



    with TestClient(app) as c:
        # CSRF: мутации с cookie требуют Origin/Referer; TestClient сам их не шлёт.
        c.headers["Origin"] = "http://testserver"
        yield c





def _login(client: TestClient, *, name: str, telegram_id: str, password: str | None = None, role: str | None = None) -> str:
    from app.app.tests.auth_helpers import access_token

    return access_token(client, name=name, telegram_id=telegram_id, password=password, role=role)


@pytest.fixture()
def admin_token(client: TestClient) -> str:
    return _login(client, name="Admin", telegram_id="tg-admin", role="admin")


@pytest.fixture()
def manager_token(client: TestClient) -> str:
    return _login(client, name="Manager", telegram_id="tg-manager", role="manager")


@pytest.fixture()
def user_token(client: TestClient) -> str:
    return _login(client, name="User", telegram_id="tg-user", role="user", password=None)





@pytest.fixture()

def auth_headers(admin_token: str) -> dict[str, str]:

    return {"Authorization": f"Bearer {admin_token}"}




@pytest.fixture()

def anon_client(client: TestClient) -> TestClient:

    """TestClient that has never called /auth/login (no session cookie)."""

    return client




@pytest.fixture()

def cookie_auth_client(client: TestClient, admin_token: str) -> TestClient:

    """Same TestClient after admin login: HttpOnly primary_analitics_session is in the cookie jar.

    admin_token is the Bearer twin of that cookie. Packet 1: cookie alone is enough
    for get_current_user; Bearer still works. Packet 2 will close anonymous reports.
    """

    return client


@pytest.fixture()
def member_card(db_engine):
    """Карточка сотрудника для выдачи доступа к проекту (Р6, PEOPLE-FLOW-PLAN ЛГ3): без неё учётку в проект не добавить."""
    import app.app.db as db_mod
    from app.app.services.companies import canonical_company, sync_companies

    db = db_mod.SessionLocal()
    try:
        sync_companies(db)
        return {"last_name": "Пример", "first_name": "Участник", "company_id": canonical_company(db, "ACME").id}
    finally:
        db.close()
