"""Чат: проверка цитат, возражения пользователя, расширение запроса (урок 16.10 — согласие под нажимом)."""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import select

from app.app.services.chat import answer_chat
from app.app.services.chat_query import claim_of, expand_query, is_pushback
from app.app.services.chat_quotes import UNVERIFIED_MARK, extract_quotes, quote_found, normalize, verify_answer_quotes

EVIDENCE = (
    "[18:05 - 21:23] Ольга (заказчик): у нас нет складского учёта. Это тоже кусок, который было бы классно "
    "реализовать, то есть сканеры, штрих-кодирование, поступление материалов.\n"
    "Выжимка: фокус смещён на 1С:УТ (анализ нетиповых сценариев номенклатуры, комплектаций). Этап 600 000 руб."
)


def test_quote_found_exact_fuzzy_and_ellipsis():
    ev = normalize(EVIDENCE)
    assert quote_found("у нас нет складского учета", ev)  # ё/е и регистр не мешают
    assert quote_found("было бы классно реализовать, то есть сканеры, штрихкодирование, поступление материалов", ev)
    assert quote_found("у нас нет складского учёта … сканеры, штрих-кодирование", ev)
    assert quote_found("Этап 600000 руб", ev)  # разряды числа
    assert not quote_found("анализ нетиповых сценариев номенклатуры и складского учета", ev)  # дописано к выжимке
    assert not quote_found("обещали прислать оценку складского учёта до понедельника", ev)


def test_extract_quotes_skips_short_terms():
    answer = "Документ «Заказ клиента». Ольга: «у нас нет складского учёта» и “было бы классно реализовать”."
    assert extract_quotes(answer) == ["у нас нет складского учёта", "было бы классно реализовать"]


def test_verify_marks_invented_quote_and_ignores_user_words():
    answer = (
        "Сказано прямо: «у нас нет складского учёта» (созвон 09.10, 18:05). "
        "В выжимке: «фокус на анализе складского учёта и сквозных статусов заказа». "
        "Вы спросили про «назвать стоимость только на аудит»."
    )
    marked, report = verify_answer_quotes(answer, EVIDENCE, user_texts=["что значит назвать стоимость только на аудит?"])
    assert report["checked"] == 3 and report["verified"] == 1 and report["from_user"] == 1
    assert report["unverified"] == ["фокус на анализе складского учёта и сквозных статусов заказа"]
    assert "сквозных статусов заказа»" + UNVERIFIED_MARK in marked
    assert "складского учёта»" + UNVERIFIED_MARK not in marked.split("В выжимке")[0]


def test_pushback_detection_and_claim():
    q = "Почему ты не сказал, что после последнего созвона обещали дать оценку на складской учет в 1с?"
    assert is_pushback(q)
    assert claim_of(q).startswith("после последнего созвона обещали дать оценку")
    assert is_pushback("так получается не шла речь о оценке складского учета?")
    assert is_pushback("Ты ошибся, срок был другой")
    assert not is_pushback("Были ли упоминания сроков внедрения складского контура?")
    assert claim_of("Что обещали после созвона?") == "Что обещали после созвона?"


def test_expand_query_adds_spoken_words():
    extra = expand_query("сроки и стоимость внедрения складского контура и сквозных статусов заказов")
    for word in ("ТСД", "штрихкодирование", "отслеживать", "смета", "месяц"):
        assert word in extra
    assert "сроки" not in extra  # уже в вопросе
    assert expand_query("как дела у команды") == []


def _project(name: str) -> tuple[int, int]:
    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.models.user import User

    db = SessionLocal()
    admin = db.scalar(select(User).limit(1))
    p = Project(name=name, description="", created_by=admin.id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    out = (p.id, admin.id)
    db.close()
    return out


def test_chat_pushback_note_and_quote_mark(client, db_engine, admin_token, tmp_data):
    """Под нажимом модель «подтверждает» несуществующую цитату — ответ помечает её, промпт просит проверить утверждение."""
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.chat_message import ChatMessage

    pid, uid = _project("QuoteCheckChat")
    settings = get_settings()
    captured: dict = {}

    def fake_completion(settings, *, system, user, max_tokens, temperature=0.2):
        captured["system"], captured["user"] = system, user
        return "Вы правы. В выжимке: «интегратор обещал оценку складского учёта до понедельника»."

    with patch("app.app.services.chat.embed_texts", return_value=[[0.1] * 1024]) as embed:
        with patch("app.app.services.chat.search_vectors", return_value=[]):
            with patch("app.app.services.chat._document_context_blocks", return_value=[]):
                with patch("app.app.services.chat._project_tz_excerpt", return_value="Выжимка: склад не обсуждали."):
                    with patch("app.app.services.chat.chat_completion_no_think", side_effect=fake_completion):
                        db = SessionLocal()
                        settings.dry_run = False
                        out = answer_chat(db, settings, pid, uid,
                                          "Почему ты не сказал, что обещали оценку на складской учет?")
                        db.close()

    assert "Проверка утверждения пользователя" in captured["user"]
    assert "«обещали оценку на складской учет»" in captured["user"]
    assert "Не соглашайся и не извиняйся" in captured["system"]
    assert "Уровни уверенности" in captured["system"]
    texts = embed.call_args.args[1]
    assert len(texts) == 2 and "ТСД" in texts[1]  # второй запрос — утверждение и слова о складе
    assert UNVERIFIED_MARK in out["answer"]
    assert out["quote_check"]["unverified"] == ["интегратор обещал оценку складского учёта до понедельника"]
    db = SessionLocal()
    row = db.scalars(select(ChatMessage).where(ChatMessage.project_id == pid, ChatMessage.role == "assistant")).first()
    assert UNVERIFIED_MARK in row.content and '"pushback": true' in row.meta_json
    db.close()
