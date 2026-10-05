"""Заготовка карты по графу: разделы, черновики из докстрингов, обновление с сохранением вписанных описаний, пробы смысла и команда `map`."""

from __future__ import annotations

import pytest

from projectmap.build import build_graph
from projectmap.cli import main
from projectmap.config import Config, parse_config
from projectmap.context import ProjectContext
from projectmap.probes import run_probes
from projectmap.scaffold import DRAFT, TODO, is_unfinished, parse_descriptions, render_document

FILES = {
    "app/models.py": ('"""Модели."""\nfrom sqlalchemy import Column, Integer, String\nfrom sqlalchemy.orm import DeclarativeBase\n\n\nclass Base(DeclarativeBase):\n    pass\n\n\n'
                      'class User(Base):\n    __tablename__ = "users"\n    id = Column(Integer, primary_key=True)\n    name = Column(String)\n'),
    "app/routes.py": ('"""Роуты пользователей."""\nimport os\n\nfrom fastapi import APIRouter\n\nfrom app.models import User\n\nrouter = APIRouter()\n\n\n'
                      '@router.get("/users")\ndef list_users():\n    """Список пользователей."""\n    return db.query(User).all()\n\n\n'
                      '@router.post("/users")\ndef add_user():\n    db.add(User(name=os.environ.get("DEFAULT_NAME", "x")))\n    db.commit()\n'),
    "app/main.py": "from fastapi import FastAPI\n\nfrom app import routes\n\napp = FastAPI()\napp.include_router(routes.router)\n",
    "docker-compose.yml": "services:\n  web:\n    image: demo:1\n    ports:\n      - '8000:8000'\n",
    ".env.example": "DEFAULT_NAME=x\n",
    "tests/test_routes.py": "def test_x():\n    pass\n",
}


def _write(root, files):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.fixture()
def project(tmp_path):
    _write(tmp_path, FILES)
    return tmp_path


@pytest.fixture()
def graph(project):
    return build_graph(project, Config())


def test_new_document_has_all_sections_with_keys_and_drafts(graph):
    text, report = render_document(graph, Config(), title="Проба")
    assert text.startswith("# Проба") and set(report["sections"]) == {"subsystems", "routes", "tables", "env", "services", "modules"}
    assert "| `GET /users` |" in text and "`list_users`" in text and f"{DRAFT} Список пользователей." in text
    assert "| `POST /users` |" in text and "пишет `users`" in text and "читает `users`" in text
    assert "| `users` |" in text and "| `DEFAULT_NAME` |" in text and "| `web` | demo:1 | 8000:8000 |" in text
    assert "#### app (3)" in text and f"{DRAFT} Роуты пользователей." in text and "test_routes" not in text.split("## Модули")[1]
    assert report["unfinished"] == report["rows"] and TODO in text


def test_update_keeps_written_descriptions_text_outside_markers_and_adds_and_removes_rows(graph):
    first, _ = render_document(graph, Config(), title="Проба")
    lines = first.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("| `users` |"):
            lines[i] = line.rsplit("|", 2)[0] + "| Учётные записи портала |"
        if line.startswith("| `GET /users` |"):
            lines.insert(i + 1, "| `GET /gone` | `app/old.py:1` `gone` | — | — | Устаревший роут |")
    edited = "\n".join(lines) + "\n\n## Мой раздел\n\nРукописный абзац, который команда не трогает.\n"
    text, report = render_document(graph, Config(), edited)
    assert "| Учётные записи портала |" in text and "Рукописный абзац" in text and "Мой раздел" in text
    assert "GET /gone" not in text and report["removed"] == 1
    again, report2 = render_document(graph, Config(), text)
    assert again == text and report2["added"] == 0 and report2["removed"] == 0, "повторный запуск ничего не меняет"
    assert parse_descriptions(text)["tables"]["users"] == "Учётные записи портала"


def test_a_new_entity_gets_a_row_in_an_existing_document(project, graph):
    old, _ = render_document(graph, Config())
    (project / "app" / "extra.py").write_text('"""Дополнительный модуль."""\n\n\ndef extra():\n    return 1\n', encoding="utf-8")
    text, report = render_document(build_graph(project, Config()), Config(), old)
    assert "| `app/extra.py` |" in text and report["added"] == 1 and f"{DRAFT} Дополнительный модуль." in text


def test_unfinished_means_empty_todo_or_draft():
    assert is_unfinished("") and is_unfinished(TODO) and is_unfinished(f"{DRAFT} из докстринга") and not is_unfinished("Учёт пользователей")


def test_pipes_in_cells_do_not_break_the_table(graph):
    node = {"id": "route:GET /a|b", "kind": "route", "label": "GET /a|b", "sub": "GET", "path": "x.py", "line": 1, "group": "x", "doc": "а | б"}
    text, _ = render_document({**graph, "nodes": [*graph["nodes"], node]}, Config())
    assert "`GET /a\\|b`" in text and parse_descriptions(text)["routes"]["GET /a|b"].endswith("а | б")


def _probes(project, graph, config, only):
    return {c.probe: c for c in run_probes(ProjectContext(project, graph, config), only).coverages}


def test_probes_count_mentions_and_written_descriptions(project, graph):
    config = parse_config({"map": {"files": ["docs/MAP.md"]}})
    none = _probes(project, graph, config, ["map_mentions", "map_described"])
    assert none["map_mentions"].total == 0 and none["map_described"].total == 0, "файла карты нет — пробы не применимы"
    text, _ = render_document(graph, config)
    (project / "docs").mkdir()
    (project / "docs" / "MAP.md").write_text(text, encoding="utf-8")
    probes = _probes(project, graph, config, ["map_mentions", "map_described"])
    assert probes["map_mentions"].covered == probes["map_mentions"].total > 0
    assert probes["map_described"].covered == 0 and probes["map_described"].total > 5
    (project / "docs" / "MAP.md").write_text(text.replace(f"{DRAFT} Список пользователей.", "Список записей"), encoding="utf-8")
    assert _probes(project, graph, config, ["map_described"])["map_described"].covered == 1


def test_map_command_writes_updates_and_checks(project, capsys):
    out = project / "docs" / "PROJECT-MAP.md"
    assert main(["map", "--root", str(project), "--check"]) == 1, "карты ещё нет — отстаёт"
    assert main(["map", "--root", str(project)]) == 0 and out.is_file()
    capsys.readouterr()
    assert main(["map", "--root", str(project), "--check"]) == 0 and "актуальна" in capsys.readouterr().out
    (project / "app" / "more.py").write_text("def more():\n    return 2\n", encoding="utf-8")
    assert main(["index", "--root", str(project)]) == 0
    assert main(["map", "--root", str(project), "--check"]) == 1 and "отстаёт" in capsys.readouterr().out


def test_route_mention_tolerates_other_parameter_names_and_shortened_tails():
    from projectmap.probes import _normal_params, _route_mentioned

    text = _normal_params("Кнопка: `POST /api/artifacts/{id}/rebuild-summary`, адрес `…/members/{user_id}/role`.")
    assert _route_mentioned("/api/artifacts/{artifact_id}/rebuild-summary", text)
    assert _route_mentioned("/projects/{project_id}/members/{user_id}/role", text)
    assert not _route_mentioned("/projects/{project_id}/other", text) and not _route_mentioned("not-a-path", text)
