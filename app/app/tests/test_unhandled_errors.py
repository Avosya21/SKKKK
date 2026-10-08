"""Необработанная ошибка API: ответ 500 без подробностей и запись в журнал портала (раньше — только консоль uvicorn)."""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient


def test_unhandled_error_is_logged_and_answered_500(db_engine, caplog):
    from app.app.main import app

    path = "/__test_unhandled_error"

    def boom():
        raise RuntimeError("сбой для проверки")

    app.add_api_route(path, boom, methods=["GET"])
    try:
        with TestClient(app, raise_server_exceptions=False) as c, caplog.at_level(logging.ERROR):
            r = c.get(path)
    finally:
        app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", None) != path]
    assert r.status_code == 500
    assert r.json() == {"detail": "Внутренняя ошибка сервера"}
    assert any("api.unhandled GET " + path in rec.getMessage() and "сбой для проверки" in rec.getMessage() for rec in caplog.records)
