from __future__ import annotations



import os

from collections.abc import Generator

from pathlib import Path



import pytest

from fastapi.testclient import TestClient

from sqlalchemy import create_engine

from sqlalchemy.orm import sessionmaker



ADMIN_PASS = "admin-pass"

MANAGER_PASS = "manager-pass"

USER_PASS = "user-pass"



# Фиксированные bcrypt-хеши — не считаем при каждом запуске pytest (см. docs/TESTING.md).

ADMIN_PASSWORD_HASH = "$2b$12$KILB8fgzE1gBya1wzii.vOBk1IMZzwRATP.q0y6wXbkgm5ceFrar6"

MANAGER_PASSWORD_HASH = "$2b$12$/WKdzAQ4tnmOPg5U5pN20Ok7F.ycnRrdYm8H1O7sEFvn5p5LuNKc."

USER_PASSWORD_HASH = "$2b$12$zfnzv12XGxxmR740LIw6nOSGNB95H8lf.tz5FFcOZZMKELKYBYQPy"



os.environ.setdefault("SECRET_KEY", "test-secret-key-32chars!!")

os.environ["ADMIN_PASSWORD_HASH"] = ADMIN_PASSWORD_HASH

os.environ["MANAGER_PASSWORD_HASH"] = MANAGER_PASSWORD_HASH

os.environ["USER_PASSWORD_HASH"] = USER_PASSWORD_HASH

os.environ["DRY_RUN"] = "1"





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

        yield c





def _login(client: TestClient, *, name: str, telegram_id: str, password: str) -> str:

    r = client.post(

        "/auth/login",

        json={"name": name, "telegram_id": telegram_id, "password": password},

    )

    assert r.status_code == 200, r.text

    return r.json()["access_token"]





@pytest.fixture()

def admin_token(client: TestClient) -> str:

    return _login(client, name="Admin", telegram_id="tg-admin", password=ADMIN_PASS)





@pytest.fixture()

def manager_token(client: TestClient) -> str:

    return _login(client, name="Manager", telegram_id="tg-manager", password=MANAGER_PASS)





@pytest.fixture()

def user_token(client: TestClient) -> str:

    return _login(client, name="User", telegram_id="tg-user", password=USER_PASS)





@pytest.fixture()

def auth_headers(admin_token: str) -> dict[str, str]:

    return {"Authorization": f"Bearer {admin_token}"}




@pytest.fixture()

def anon_client(client: TestClient) -> TestClient:

    """TestClient that has never called /auth/login (no session cookie)."""

    return client




@pytest.fixture()

def cookie_auth_client(client: TestClient, admin_token: str) -> TestClient:

    """Same TestClient after admin login: HttpOnly skkkk_session is in the cookie jar.

    admin_token is the Bearer twin of that cookie. Packet 1: cookie alone is enough
    for get_current_user; Bearer still works. Packet 2 will close anonymous reports.
    """

    return client

