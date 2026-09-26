"""Карточка пользователя: компании из файла, поля, фото, склейка дублей (сервисы без HTTP)."""

from __future__ import annotations

import io

import pytest
from PIL import Image


def jpeg(width=800, height=600, color=(200, 30, 30), *, orientation=None, maker=None) -> bytes:
    image = Image.new("RGB", (width, height), color)
    image.paste((10, 10, 250), (0, 0, width // 4, height))  # левая полоса — видно, куда повернули
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    if maker:
        exif[0x010F] = maker  # Make
        exif[0x0110] = "Model X"  # Model
    buf = io.BytesIO()
    image.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


@pytest.fixture()
def db(db_engine):
    import app.app.db as db_mod

    session = db_mod.SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def people(tmp_data, monkeypatch):
    from app.app.config import get_settings

    get_settings.cache_clear()
    return tmp_data / "people"


def _user(db, name="Admin"):
    from app.app.models.user import User

    user = User(name=name, telegram_id=f"tg-{name}", role="user", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _companies(db):
    from app.app.services.companies import sync_companies

    return {c.name: c for c in sync_companies(db)}


# --- компании ---


def test_parse_companies_lines():
    from app.app.services.companies import parse_companies

    lines = parse_companies(
        "# комментарий\n\n  ACME   |  Исполнитель \nАльфа|заказчик\nБета\nГамма | партнёр\nacme | Заказчик\n"
    )
    assert [(c.name, c.side) for c in lines] == [
        ("ACME", "executor"),
        ("Альфа", "customer"),
        ("Бета", ""),
        ("Гамма", ""),  # неизвестная пометка — без стороны (и предупреждение в лог)
    ]


def test_sync_adds_updates_hides_and_returns(db, tmp_path):
    from app.app.models.company import Company
    from app.app.services.companies import sync_companies

    path = tmp_path / "c.txt"
    path.write_text("ACME | Исполнитель\nАльфа | Заказчик\n", encoding="utf-8")
    assert [c.name for c in sync_companies(db, path)] == ["ACME", "Альфа"]

    path.write_text("Альфа | Исполнитель\n", encoding="utf-8")
    active = sync_companies(db, path)
    assert [(c.name, c.side) for c in active] == [("Альфа", "executor")]
    hidden = db.query(Company).filter_by(name_key="acme").one()
    assert hidden.active is False  # не удалена: у людей компания остаётся

    path.write_text("Acme | Исполнитель\nАльфа | Исполнитель\n", encoding="utf-8")
    active = sync_companies(db, path)
    assert [c.name for c in active] == ["Acme", "Альфа"]
    assert db.query(Company).count() == 2


def test_default_companies_file_from_settings(db):
    companies = _companies(db)
    assert companies["ACME"].side == "executor"
    assert companies["Заказчик Тест"].side == "customer"


# --- поля ---


@pytest.mark.parametrize(
    ("last", "first", "job", "field"),
    [
        ("", "Иван", "Аналитик", "last_name"),
        ("Иванов", "  ", "Аналитик", "first_name"),
        ("Иванов2", "Иван", "Аналитик", "last_name"),
        ("Иванов", "<b>Иван</b>", "Аналитик", "first_name"),
        ("Иванов", "Иван", "A", "job_title"),
        ("И" * 101, "Иван", "Аналитик", "last_name"),
    ],
)
def test_fields_rules(last, first, job, field):
    from app.app.services.user_profile import ProfileError, clean_fields

    with pytest.raises(ProfileError) as exc:
        clean_fields(last, first, job)
    assert exc.value.field == field


def test_fields_accept_hyphen_apostrophe_latin_and_trim():
    from app.app.services.user_profile import clean_fields

    fields = clean_fields("  Римский-Корсаков ", "Д’Артаньян", "  ведущий   аналитик ")
    assert (fields.last_name, fields.first_name, fields.job_title) == (
        "Римский-Корсаков",
        "Д’Артаньян",
        "ведущий аналитик",
    )
    assert clean_fields("O'Neil", "John", "PM").last_name == "O'Neil"


# --- заполнение, правка, админ ---


def test_complete_onboarding_saves_all_and_consent(db, people):
    from app.app.services import user_profile as up

    user = _user(db)
    company = _companies(db)["ACME"]
    profile = up.complete_onboarding(
        db, user, last_name="Иванов", first_name="Иван", job_title="Аналитик",
        company_id=company.id, avatar_bytes=jpeg(),
    )
    assert up.is_complete(profile)
    assert up.full_name(profile) == "Иван Иванов"
    assert profile.voice_consent_at is not None
    assert profile.voice_consent_text_version == up.CONSENT_TEXT_VERSION
    assert (people / str(user.id) / f"avatar-{profile.avatar_key}-512.webp").is_file()


def test_onboarding_requires_photo_and_active_company(db, people, tmp_path):
    from app.app.services import user_profile as up
    from app.app.services.companies import sync_companies

    user = _user(db)
    company = _companies(db)["ACME"]
    kwargs = dict(last_name="Иванов", first_name="Иван", job_title="Аналитик")
    with pytest.raises(up.ProfileError) as exc:
        up.complete_onboarding(db, user, company_id=company.id, avatar_bytes=b"", **kwargs)
    assert exc.value.field == "avatar"
    with pytest.raises(up.ProfileError) as exc:
        up.complete_onboarding(db, user, company_id=None, avatar_bytes=jpeg(), **kwargs)
    assert exc.value.field == "company_id"
    path = tmp_path / "only.txt"
    path.write_text("Другая | Заказчик\n", encoding="utf-8")
    sync_companies(db, path)  # ACME скрыта
    with pytest.raises(up.ProfileError):
        up.complete_onboarding(db, user, company_id=company.id, avatar_bytes=jpeg(), **kwargs)
    assert up.get_profile(db, user.id) is None  # ничего не сохранилось наполовину


def test_update_own_does_not_touch_company(db, people):
    from app.app.services import user_profile as up

    user = _user(db)
    companies = _companies(db)
    up.complete_onboarding(
        db, user, last_name="Иванов", first_name="Иван", job_title="Аналитик",
        company_id=companies["ACME"].id, avatar_bytes=jpeg(),
    )
    profile = up.update_own(db, user, last_name="Петров", first_name="Пётр", job_title="Руководитель")
    assert (profile.last_name, profile.company_id) == ("Петров", companies["ACME"].id)
    profile = up.set_company(db, user.id, companies["Заказчик Тест"].id)
    assert profile.company_id == companies["Заказчик Тест"].id


def test_reset_keeps_admin_company_and_asks_again(db, people):
    from app.app.services import user_profile as up

    user = _user(db)
    companies = _companies(db)
    first = up.complete_onboarding(
        db, user, last_name="Иванов", first_name="Иван", job_title="Аналитик",
        company_id=companies["ACME"].id, avatar_bytes=jpeg(),
    )
    old_key = first.avatar_key
    up.set_company(db, user.id, companies["Заказчик Тест"].id)
    up.reset_profile(db, user.id)
    profile = up.get_profile(db, user.id)
    assert not up.is_complete(profile)
    assert not (people / str(user.id) / f"avatar-{old_key}-512.webp").exists()
    # При повторном заполнении выбор компании игнорируется — её назначил админ.
    again = up.complete_onboarding(
        db, user, last_name="Иванов", first_name="Иван", job_title="Аналитик",
        company_id=companies["ACME"].id, avatar_bytes=jpeg(color=(1, 2, 3)),
    )
    assert again.company_id == companies["Заказчик Тест"].id


def test_withdraw_and_restore_consent(db, people):
    from app.app.services import user_profile as up

    user = _user(db)
    up.complete_onboarding(
        db, user, last_name="Иванов", first_name="Иван", job_title="Аналитик",
        company_id=_companies(db)["ACME"].id, avatar_bytes=jpeg(),
    )
    up.withdraw_voice_consent(db, user.id)
    profile = up.get_profile(db, user.id)
    assert profile.voice_consent_at is None and profile.voice_consent_text_version is None
    up.restore_voice_consent(db, user.id)
    db.refresh(profile)
    assert profile.voice_consent_at is not None


# --- склейка дублей ---


def _complete(db, user, color):
    from app.app.services import user_profile as up

    return up.complete_onboarding(
        db, user, last_name="Иванов", first_name=f"Имя{'а' * color[0]}".replace("0", ""), job_title="Аналитик",
        company_id=_companies(db)["ACME"].id, avatar_bytes=jpeg(color=color),
    )


@pytest.mark.parametrize("case", ["loser_only", "winner_only", "both_winner_newer", "both_loser_newer"])
def test_merge_users_keeps_one_card_and_photo(db, people, case):
    from app.app.models.user_profile import UserProfile
    from app.app.services import user_profile as up
    from app.app.services.user_identity import merge_users_into

    winner, loser = _user(db, "Winner"), _user(db, "Loser")
    w = l = None
    if case == "loser_only":
        l = _complete(db, loser, (1, 1, 1))
    elif case == "winner_only":
        w = _complete(db, winner, (2, 2, 2))
    elif case == "both_winner_newer":
        l = _complete(db, loser, (1, 1, 1))
        w = _complete(db, winner, (2, 2, 2))
    else:
        w = _complete(db, winner, (2, 2, 2))
        l = _complete(db, loser, (1, 1, 1))
    expected_key = {
        "loser_only": l and l.avatar_key,
        "winner_only": w and w.avatar_key,
        "both_winner_newer": w and w.avatar_key,
        "both_loser_newer": l and l.avatar_key,
    }[case]
    winner_id, loser_id = winner.id, loser.id
    merge_users_into(db, winner, [loser])
    db.expire_all()
    assert db.query(UserProfile).count() == 1
    kept = up.get_profile(db, winner_id)
    assert kept.avatar_key == expected_key
    assert (people / str(winner_id) / f"avatar-{expected_key}-96.webp").is_file()
    assert not (people / str(loser_id)).exists()
