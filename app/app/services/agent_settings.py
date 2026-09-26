"""Настройки проекта для обмена с внешними агентами (хранятся в Project.settings_json → "agent_exchange").

Зачем: контракт не должен быть привязан к одному проекту и стеку 1С/Битрикс24. Что настраивается:
- participants — явные стороны участников (заказчик/исполнитель); важнее эвристики по роли;
- side_hints — слова в ролях, по которым определяется сторона (названия компаний), вместо зашитых в код;
- timezone — пояс, в котором записаны время в переписках («+03:00»);
- domain_pack — словарь якорей кода (`1c_bitrix24`, `none`);
- exchange_enabled / auto_answer — включён ли обмен и автоответ локальной модели.
Совместимость: у проектов, где раздела нет, значения прежние (LEGACY), поведение контракта не меняется. У новых проектов записывается NEW_PROJECT.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

from app.app.models.project import Project

KEY = "agent_exchange"
PACKS_DIR = Path(__file__).resolve().parents[1] / "data" / "anchor_packs"
SIDES = ("customer", "vendor")

# Поведение проектов, созданных до появления настроек (проект «Пилот»): прежние правила, ничего не меняется.
LEGACY: dict[str, Any] = {
    "participants": [], "side_hints": {"customer": ["эталон"], "vendor": ["акме"]}, "timezone": "+03:00",
    "domain_pack": "1c_bitrix24", "exchange_enabled": True, "auto_answer": False, "default_key_scope": "read",
}
# Новый проект: без якорей и без названий компаний в правилах, пояс задаёт владелец.
NEW_PROJECT: dict[str, Any] = {
    "participants": [], "side_hints": {"customer": [], "vendor": []}, "timezone": "+03:00",
    "domain_pack": "none", "exchange_enabled": True, "auto_answer": False, "default_key_scope": "read",
}
_TZ_RE = re.compile(r"^([+-])(\d{2}):(\d{2})$")


class SettingsError(ValueError):
    pass


def available_packs() -> list[dict[str, str]]:
    out = []
    for path in sorted(PACKS_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append({"name": data["name"], "title": data.get("title", data["name"]), "description": data.get("description", "")})
    return out


def load_pack_rules(name: str) -> list[tuple[re.Pattern, str, str, str]]:
    path = PACKS_DIR / f"{name}.json"
    if not re.fullmatch(r"[a-z0-9_]{1,40}", name or "") or not path.is_file():
        raise SettingsError(f"Неизвестный пакет якорей: {name}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return [(re.compile(r["pattern"], re.I), r["system"], r["object"], r["hint"]) for r in data.get("rules", [])]


def parse_tz(value: str) -> timezone:
    m = _TZ_RE.match(value or "")
    if not m or int(m.group(2)) > 14 or int(m.group(3)) > 59:
        raise SettingsError("Часовой пояс задаётся как +03:00")
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return timezone(delta if m.group(1) == "+" else -delta)


def get_settings_for(project: Project) -> dict[str, Any]:
    raw = project.settings().get(KEY)
    base = LEGACY if raw is None else NEW_PROJECT
    merged = {**base, **(raw or {})}
    merged["side_hints"] = {**base["side_hints"], **(merged.get("side_hints") or {})}
    return merged


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Проверка и очистка присланных настроек (значения обрезаются, лишние ключи отбрасываются)."""
    out: dict[str, Any] = {}
    if "participants" in data:
        rows = data["participants"]
        if not isinstance(rows, list) or len(rows) > 200:
            raise SettingsError("participants: список до 200 записей")
        clean = []
        for row in rows:
            name = str((row or {}).get("name", "")).strip()[:120]
            side = (row or {}).get("side")
            if not name or side not in SIDES:
                raise SettingsError("participants: нужны name и side (customer или vendor)")
            clean.append({"name": name, "side": side})
        out["participants"] = clean
    if "side_hints" in data:
        hints = data["side_hints"] or {}
        out["side_hints"] = {s: [str(w).strip().lower()[:40] for w in (hints.get(s) or []) if str(w).strip()][:20] for s in SIDES}
    if "timezone" in data:
        parse_tz(str(data["timezone"]))
        out["timezone"] = str(data["timezone"])
    if "domain_pack" in data:
        load_pack_rules(str(data["domain_pack"]))
        out["domain_pack"] = str(data["domain_pack"])
    if "default_key_scope" in data:
        from app.app.services.project_mcp_tokens import SCOPES

        if data["default_key_scope"] not in SCOPES:
            raise SettingsError(f"default_key_scope: допустимо {', '.join(SCOPES)}")
        out["default_key_scope"] = data["default_key_scope"]
    for flag in ("exchange_enabled", "auto_answer"):
        if flag in data:
            out[flag] = bool(data[flag])
    return out


def save(project: Project, data: dict[str, Any]) -> dict[str, Any]:
    current = project.settings()
    cur = get_settings_for(project)
    cur.update(normalize(data))
    current[KEY] = cur
    project.set_settings(current)
    return cur


def init_new_project(project: Project) -> None:
    """Новому проекту — явные значения по умолчанию (без якорей 1С)."""
    current = project.settings()
    current.setdefault(KEY, json.loads(json.dumps(NEW_PROJECT)))
    project.set_settings(current)
