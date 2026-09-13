from sqlalchemy import func, select

from app.app.db import Base
from app.app.models.artifact import Artifact
from app.app.models.chat_message import ChatMessage
from app.app.models.project import Project
from app.app.models.user import User
from app.app.services.user_identity import _USER_ID_COLUMNS, merge_duplicate_logins, pick_canonical_user


def test_same_name_different_telegram_reuses_user(client, db_engine, admin_token):
    from app.app.db import SessionLocal

    db = SessionLocal()
    admin = db.scalar(select(User).where(func.lower(User.name) == "admin"))
    assert admin is not None
    first_id = admin.id
    first_count = db.scalar(select(func.count()).select_from(User).where(func.lower(User.name) == "admin"))
    db.close()

    r = client.post(
        "/auth/login",
        json={"name": "Admin", "telegram_id": "tg-admin-other", "password": "admin-pass"},
    )
    assert r.status_code == 200, r.text

    db2 = SessionLocal()
    again = db2.scalar(select(User).where(func.lower(User.name) == "admin"))
    count = db2.scalar(select(func.count()).select_from(User).where(func.lower(User.name) == "admin"))
    assert again.id == first_id
    assert again.telegram_id == "tg-admin-other"
    assert count == first_count
    db2.close()


def test_merge_duplicate_logins_keeps_chat_owner(client, db_engine, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from sqlalchemy import text

    db = SessionLocal()
    db.execute(text("DROP INDEX IF EXISTS ux_users_name_nocase"))
    db.commit()
    a = User(name="demo_user", telegram_id="dup-a", role="admin", password_hash="x")
    b = User(name="Demo_User", telegram_id="dup-b", role="admin", password_hash="x")
    c = User(name="demo_user", telegram_id="dup-c", role="admin", password_hash="x")
    db.add_all([a, b, c])
    db.commit()
    db.refresh(a)
    db.refresh(b)
    db.refresh(c)
    p = Project(name="MergeChat", description="", created_by=a.id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    db.add(ChatMessage(project_id=p.id, user_id=b.id, role="user", content="старый вопрос", waiting=False))
    db.add(
        Artifact(
            project_id=p.id,
            user_id=c.id,
            artifact_id="art_merge_dup",
            original_filename="f.txt",
            stored_filename="f.txt",
            file_path="/tmp/f.txt",
            sha256="mergehash",
            file_type="txt",
            source_type="document",
            status="done",
        )
    )
    db.commit()
    winner = pick_canonical_user(db, [a, b, c])
    assert winner.id == b.id
    n = merge_duplicate_logins(db)
    assert n >= 2
    left = list(db.scalars(select(User).where(func.lower(User.name) == "demo_user")).all())
    assert len(left) == 1
    assert left[0].id == b.id
    chats = list(db.scalars(select(ChatMessage).where(ChatMessage.user_id == b.id)).all())
    assert any(m.content == "старый вопрос" for m in chats)
    art = db.scalar(select(Artifact).where(Artifact.artifact_id == "art_merge_dup"))
    assert art.user_id == b.id
    db.close()


def test_merge_covers_all_user_foreign_keys():
    import app.app.models  # noqa: F401

    expected = set()
    for table in Base.metadata.tables.values():
        for col in table.columns:
            for fk in col.foreign_keys:
                if fk.column.table.name == "users" and fk.column.name == "id":
                    expected.add((table.name, col.name))
    actual = {(model.__tablename__, column) for model, column in _USER_ID_COLUMNS}
    assert actual == expected, f"merge columns {actual} vs FKs {expected}"


def test_failed_login_does_not_merge_duplicates(client, db_engine):
    from app.app.db import SessionLocal
    from sqlalchemy import text

    db = SessionLocal()
    db.execute(text("DROP INDEX IF EXISTS ux_users_name_nocase"))
    db.commit()
    db.add_all(
        [
            User(name="dup_login_keep", telegram_id="dup-keep-a", role="admin", password_hash="x"),
            User(name="dup_login_keep", telegram_id="dup-keep-b", role="admin", password_hash="x"),
        ]
    )
    db.commit()
    before = db.scalar(select(func.count()).select_from(User).where(func.lower(User.name) == "dup_login_keep"))
    db.close()
    assert before == 2

    r = client.post(
        "/auth/login",
        json={"name": "dup_login_keep", "telegram_id": "dup-keep-new", "password": "wrong-pass"},
    )
    assert r.status_code == 401

    db2 = SessionLocal()
    after = db2.scalar(select(func.count()).select_from(User).where(func.lower(User.name) == "dup_login_keep"))
    db2.close()
    assert after == 2
