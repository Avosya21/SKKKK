"""Полнота карты: всё, что лежит на стенде, описано в инвентаре, и раздел §17 карты не отстаёт.

Страж к `scripts/check_stand_inventory.py`: если на диск положили что-то новое (окружение, инструмент,
каталог данных, модель) и не описали — тест падает с точным списком.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_stand_inventory.py"
MAP = ROOT / "docs" / "PROJECT-MAP.md"


def _inventory():
    spec = importlib.util.spec_from_file_location("check_stand_inventory", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _norm(text: str) -> str:
    """Убрать из сравнения машинно-зависимые строки: отметку времени и список отсутствующих необязательных."""
    return "\n".join(line for line in text.splitlines()
                     if not line.startswith("_Проверено") and not line.startswith("_Нет на диске"))


def test_everything_on_the_stand_is_described():
    module = _inventory()
    report = module.check()
    assert report["unknown"] == [], f"на диске есть сущности вне инвентаря: {report['unknown']}"
    assert report["absent"] == [], f"в инвентаре описаны, но пропали: {report['absent']}"
    assert report["total"] > 80, "инвентарь подозрительно короткий — проверьте docs/project-map/stand.yaml"


def test_inventory_covers_every_layer_of_the_stand():
    module = _inventory()
    rows = module.check()["rows"]
    kinds = {row["kind"] for row in rows}
    for expected in ("окружение", "БД", "граф", "модель", "сервис", "данные"):
        assert expected in kinds, f"в инвентаре нет ни одной сущности вида «{expected}»"
    paths = {row["path"] for row in rows}
    assert {"venvs/lab", "data/app.db", "tools/livekit", "asr/engines.json"} <= paths


def test_map_section_matches_inventory():
    module = _inventory()
    report = module.check()
    text = MAP.read_text(encoding="utf-8")
    assert module.BEGIN in text and module.END in text, "в карте нет раздела инвентаря стенда (§17)"
    assert _norm(module.render_docs(report)) in _norm(text), \
        "раздел §17 отстал от инвентаря: venv\\Scripts\\python.exe scripts\\check_stand_inventory.py --docs"


def test_machine_facts_are_listed():
    module = _inventory()
    names = " ".join(item.get("name", "") for item in module.check()["machine"])
    for expected in ("Ollama", "LM Studio", "Docker", "Caddy", "LiveKit", "Playwright", "Android", "Git LFS"):
        assert expected in names, f"в разделе «Машина» нет «{expected}»"
