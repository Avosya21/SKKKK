"""Граф зависимостей проекта: все сущности и все связи между ними — из индекса (`data/project_map.db`) и карты.

Слои одного графа (узлы и рёбра каждого слоя помечены, поэтому их можно включать и выключать по отдельности):

| Слой | Узлы | Рёбра |
|---|---|---|
| код | файл, модуль, класс, функция, метод, константа, тест | `defines` (файл → символ), `calls` / `calls_ref` / `calls_ext` (вызов, передача как значение, внешний вызов) |
| связи | файл, символ | `imports` / `imports_ext`, `covers` (тест → модуль/символ), `changed` (коммит → файл) |
| данные | таблица БД (+ столбцы по флагу) | `reads` / `writes` (символ → таблица), `has_column` |
| интерфейс | роут, элемент UI, JS-символ, шаблон | `handles`, `guarded_by`, `depends_on`, `declares`, `handled_by`, `calls_api` |
| окружение | переменная окружения / константа | `reads_env` |
| смысл | узел манифеста карты (процесс, docker, страница, паттерн) | `m:<relation>` — рёбра `docs/project-map/manifest.yaml` |

Модуль только читает: сборка идёт из готового индекса, запись артефактов — `scripts/export_project_graph.py`
(`data/project_map_graph.json` — полный граф, `docs/project-map/graph-modules.json` — свод по модулям,
`docs/project-map/DEPENDENCIES.md` — атлас для человека, `--graphml` / `--dot` — для Gephi и yEd).

Ответы — с адресом `путь:строка`: у каждого узла есть `path`, у каждого ребра — `line` вызова или объявления.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable
from xml.sax.saxutils import escape as xml_escape

from app.app.services.project_index.config import ROOT

MANIFEST_PATH = ROOT / "docs" / "project-map" / "manifest.yaml"
GRAPH_PATH = ROOT / "data" / "project_map_graph.json"

FUNCTION_KINDS = {"function", "method", "async function", "async method"}
# Упоминания ищем только во фрагментах кода: документация (в том числе сгенерированный атлас со списком сирот)
# не должна гасить находку — иначе список сам себя стирает на следующей индексации.
MENTION_CHUNK_KINDS = ("function", "method", "test", "class", "module", "overview", "js", "js_top",
                       "template", "ui_element", "config", "css", "code")
INDEX_URL_ARG = re.compile(r"\$?\{[^}]*\}")
INDEX_URL_NUM = re.compile(r"/\d+(?=/|$)")
DEFAULT_ORPHAN_LIMIT = 60

# Подсистемы — от самой узкой к самой широкой: файл относится к первой подошедшей.
SUBSYSTEMS: tuple[tuple[str, str], ...] = (
    ("app/app/services/project_index", "карта проекта"),
    ("app/app/routers", "роутеры"),
    ("app/app/services", "сервисы"),
    ("app/app/models", "модели БД"),
    ("app/app/workers", "воркер и конвейеры"),
    ("app/app/static", "фронтенд"),
    ("app/app/templates", "шаблоны"),
    ("app/app/mcp", "MCP портала"),
    ("app/app/tests", "тесты"),
    ("app/app", "ядро API"),
    ("asr/lab/stages", "стадии ASR"),
    ("asr/lab", "лаборатория ASR"),
    ("asr/tests", "тесты ASR"),
    ("asr", "ASR-скрипты"),
    ("scripts/smoke", "смоук портала"),
    ("scripts", "служебные скрипты"),
    ("tools/livekit", "комната LiveKit"),
    ("tools/lightrag", "LightRAG"),
    ("tools", "инструменты"),
    ("docs/prompts", "промпты LLM"),
    ("docs/plans", "планы"),
    ("docs/stages", "этапы"),
    ("docs/project-map", "карта проекта"),
    ("docs", "документация"),
)

NODE_LABELS = {
    "file": "файл", "symbol": "символ", "route": "роут", "table": "таблица БД", "column": "столбец",
    "env": "окружение", "ui": "элемент интерфейса", "external": "внешнее", "commit": "коммит",
    "manifest": "узел карты",
}

RELATION_LABELS = {
    "defines": "определяет", "calls": "вызывает", "calls_ref": "передаёт как значение",
    "calls_ext": "вызывает внешнее", "imports": "импортирует", "imports_ext": "импортирует внешнее",
    "handles": "обрабатывает", "guarded_by": "защищён", "depends_on": "зависит от",
    "reads": "читает", "writes": "пишет", "has_column": "имеет столбец", "reads_env": "читает переменную",
    "declares": "объявляет", "handled_by": "обрабатывается в JS", "calls_api": "вызывает API",
    "covers": "покрывает тестом", "changed": "изменил",
}


# ───────────────────────────── утилиты ─────────────────────────────

def subsystem(path: str) -> str:
    """Подсистема файла: `app/app/services/tz.py` → «сервисы»."""
    for prefix, name in SUBSYSTEMS:
        if path == prefix or path.startswith(prefix + "/"):
            return name
    return path.split("/", 1)[0] or "корень"


def normalize_url(url: str) -> str:
    """`/projects/18/x?y=1`, `/projects/{pid}/x` и `/projects/{project_id}/x` — один шаблон."""
    url = (url or "").split("?", 1)[0]
    url = INDEX_URL_ARG.sub("{}", url)
    url = INDEX_URL_NUM.sub("/{}", url)
    return url.rstrip("/") or "/"


def _split_list(value: str | None) -> list[str]:
    """Строка индекса со списком (`get_db, get_settings`, `POST /chat GET /x`) → список значений."""
    if not value:
        return []
    if value.strip().startswith("["):
        try:
            return [str(x) for x in json.loads(value)]
        except json.JSONDecodeError:
            pass
    return [p for p in re.split(r"[,\s]+", value.strip()) if p]


def _symbol_id(path: str, qualname: str) -> str:
    return f"sym:{path}::{qualname}"


def resolve_import(path: str, module: str, names: str | None, known: set[str]) -> list[str]:
    """Какой файл проекта импортирует строка таблицы `imports`: абсолютный, относительный или `from . import x`."""
    base = path.rsplit("/", 1)[0]
    candidates: list[str] = []
    if (module or "").startswith("."):
        level = len(module) - len(module.lstrip("."))
        rest = module.lstrip(".").replace(".", "/")
        parts = base.split("/")
        prefix = "/".join(parts[: len(parts) - (level - 1)]) if level > 1 else base
        if rest:
            candidates.append(f"{prefix}/{rest}".strip("/"))
        else:
            candidates.append(prefix)
            candidates += [f"{prefix}/{name}" for name in _split_list(names)]
    else:
        dotted = (module or "").replace(".", "/")
        candidates += [dotted, f"app/{dotted}"]
        candidates += [f"{dotted}/{name}" for name in _split_list(names)]
        candidates += [f"app/{dotted}/{name}" for name in _split_list(names)]
    out: list[str] = []
    for candidate in candidates:
        for suffix in (".py", ".js", ""):
            full = (candidate + suffix).strip("/")
            if full in known:
                out.append(full)
                break
        if out:
            break
    if not out and not (module or "").startswith("."):
        # `from app.app.services import tz` — импортирован сам модуль пакета, а не имя из него
        for name in _split_list(names):
            dotted = f"{module}.{name}".replace(".", "/")
            for full in (dotted + ".py", "app/" + dotted + ".py"):
                if full in known:
                    out.append(full)
                    break
            if out:
                break
    return list(dict.fromkeys(out))


def _file_id(path: str) -> str:
    return f"file:{path}"


def _node_id(kind: str, key: str) -> str:
    return f"{kind}:{key}"


# ───────────────────────────── сборка ─────────────────────────────

class _Builder:
    def __init__(self, conn: sqlite3.Connection, *, include_external: bool, include_history: bool,
                 include_manifest: bool, with_columns: bool) -> None:
        self.conn = conn
        self.include_external = include_external
        self.include_history = include_history
        self.include_manifest = include_manifest
        self.with_columns = with_columns
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple[str, str, str], dict] = {}
        self.by_name: dict[str, list[str]] = {}
        self.by_path_name: dict[tuple[str, str], str] = {}
        self.symbols_by_path: dict[str, list[sqlite3.Row]] = {}
        self.routes_by_url: dict[str, list[str]] = {}
        self.notes: list[str] = []

    # узлы и рёбра
    def node(self, node_id: str, *, kind: str, label: str, sub: str = "", path: str = "",
             line: int | None = None, **attrs: Any) -> str:
        node = self.nodes.get(node_id)
        if node is None:
            node = {"id": node_id, "kind": kind, "label": label, "sub": sub, "path": path, "line": line,
                    "group": subsystem(path) if path else "", "layer": kind}
            self.nodes[node_id] = node
        for key, value in attrs.items():
            if value not in (None, "", [], {}):
                node[key] = value
        return node_id

    def annotate(self, node_id: str, **attrs: Any) -> None:
        """Дописать атрибуты уже созданному узлу (например, теги и вид из манифеста карты)."""
        node = self.nodes.get(node_id)
        if node is None:
            return
        for key, value in attrs.items():
            if value not in (None, "", [], {}):
                node[key] = value

    def edge(self, source: str | None, target: str | None, relation: str, line: int | None = None, **attrs: Any) -> None:
        if not source or not target or source == target:
            return
        key = (source, target, relation)
        existing = self.edges.get(key)
        if existing is None:
            self.edges[key] = {"from": source, "to": target, "rel": relation, "line": line or 0, "n": 1, **attrs}
            return
        existing["n"] += 1
        if not existing.get("line") and line:
            existing["line"] = line
        for name, value in attrs.items():
            if value:
                existing[name] = max(existing.get(name, 0), value) if isinstance(value, int) else value

    # сборка слоёв
    def files(self) -> None:
        rows = self.conn.execute("SELECT path, kind FROM files ORDER BY path").fetchall()
        for row in rows:
            self.node(_file_id(row["path"]), kind="file", label=row["path"].rsplit("/", 1)[-1],
                      sub=row["kind"] or "file", path=row["path"])

    def symbols(self) -> None:
        rows = self.conn.execute(
            "SELECT name, qualname, kind, path, line, end_line, lang, signature FROM symbols "
            "WHERE lang IS NULL OR lang != 'ps1' ORDER BY path, line"
        ).fetchall()
        for row in rows:
            path, qual, kind = row["path"], row["qualname"], row["kind"] or "symbol"
            self.symbols_by_path.setdefault(path, []).append(row)
            if kind == "module":  # модуль — это и есть файл, отдельный узел не нужен
                self.by_path_name[(path, qual)] = _file_id(path)
                continue
            node_id = self.node(_symbol_id(path, qual), kind="symbol", label=row["name"] or qual, sub=kind,
                                path=path, line=row["line"], lang=row["lang"] or "py", sig=(row["signature"] or "")[:160])
            self.by_path_name[(path, qual)] = node_id
            self.by_name.setdefault(row["name"] or qual, []).append(node_id)
            self.edge(_file_id(path), node_id, "defines", row["line"])

    def _resolve_symbol(self, path: str, name: str) -> tuple[str | None, int]:
        """Символ по имени: сначала в том же файле, затем единственное определение в проекте."""
        local = self.by_path_name.get((path, name))
        if local:
            return local, 0
        candidates = self.by_name.get(name) or []
        if not candidates:
            return None, 0
        if len(candidates) == 1:
            return candidates[0], 0
        return sorted(candidates)[0], len(candidates)

    def calls(self) -> None:
        externals: dict[str, int] = {}
        rows = self.conn.execute("SELECT path, caller, callee, qualifier, line FROM calls").fetchall()
        for row in rows:
            source = self.by_path_name.get((row["path"], row["caller"])) or _file_id(row["path"])
            target, ambiguous = self._resolve_symbol(row["path"], row["callee"])
            if target is None:
                externals[row["callee"]] = externals.get(row["callee"], 0) + 1
                if self.include_external:
                    ext = self.node(_node_id("external", row["callee"]), kind="external", label=row["callee"],
                                    sub="вызов", path="")
                    self.edge(source, ext, "calls_ext", row["line"])
                continue
            relation = "calls_ref" if row["qualifier"] == "<ref>" else "calls"
            self.edge(source, target, relation, row["line"], **({"amb": ambiguous, "cand": ambiguous} if ambiguous else {}))
        if externals:
            self.notes.append("внешних вызовов: " + str(sum(externals.values())) + " у " + str(len(externals)) + " имён")

    def imports(self) -> None:
        for row in self.conn.execute("SELECT path, module, names, line FROM imports").fetchall():
            for target_path in self._resolve_import(row["path"], row["module"], row["names"]):
                self.edge(_file_id(row["path"]), _file_id(target_path), "imports", row["line"])
            if self.include_external and not self._resolve_import(row["path"], row["module"], row["names"]):
                module = (row["module"] or "").split(".")[0]
                if module:
                    ext = self.node(_node_id("external", module), kind="external", label=module, sub="модуль", path="")
                    self.edge(_file_id(row["path"]), ext, "imports_ext", row["line"])

    def _resolve_import(self, path: str, module: str, names: str | None) -> list[str]:
        return resolve_import(path, module, names, self._known_files())

    def _known_files(self) -> set[str]:
        if not hasattr(self, "_known"):
            self._known = {row[0] for row in self.conn.execute("SELECT path FROM files").fetchall()}
        return self._known  # type: ignore[attr-defined]

    def routes(self) -> None:
        for row in self.conn.execute(
                "SELECT method, url, handler, path, line, deps, guards, doc FROM routes ORDER BY url").fetchall():
            node_id = self.node(_node_id("route", f'{row["method"]} {row["url"]}'), kind="route",
                                label=f'{row["method"]} {row["url"]}', sub=row["method"], path=row["path"],
                                line=row["line"], handler=row["handler"], guards=row["guards"] or "",
                                deps=row["deps"] or "", doc=(row["doc"] or "")[:160])
            self.routes_by_url.setdefault(normalize_url(row["url"]), []).append(node_id)
            handler = self.by_path_name.get((row["path"], row["handler"])) or self._resolve_symbol(row["path"], row["handler"])[0]
            self.edge(node_id, handler, "handles", row["line"])
            for dep in _split_list(row["deps"]):
                self.edge(node_id, self._resolve_symbol(row["path"], dep)[0], "depends_on", row["line"])
            for guard in _split_list(row["guards"]):
                self.edge(node_id, self._resolve_symbol(row["path"], guard)[0], "guarded_by", row["line"])

    def tables(self) -> None:
        tables = self.conn.execute("SELECT DISTINCT tbl FROM db_columns ORDER BY tbl").fetchall()
        for row in tables:
            tbl = row["tbl"]
            cols = self.conn.execute("SELECT col, ctype FROM db_columns WHERE tbl=? ORDER BY col", (tbl,)).fetchall()
            access = self.conn.execute("SELECT writes FROM table_access WHERE tbl=?", (tbl,)).fetchall()
            writers = sum(1 for a in access if a["writes"])
            self.node(_node_id("table", tbl), kind="table", label=tbl, sub="БД", path="",
                      cols=len(cols), writers=writers, readers=len(access) - writers)
            if self.with_columns:
                for col in cols:
                    col_id = self.node(_node_id("column", f'{tbl}.{col["col"]}'), kind="column",
                                       label=col["col"], sub=col["ctype"] or "column", path="")
                    self.edge(_node_id("table", tbl), col_id, "has_column")

    def table_access(self) -> None:
        for row in self.conn.execute("SELECT tbl, path, symbol, line, writes FROM table_access").fetchall():
            source = self.by_path_name.get((row["path"], row["symbol"])) or _file_id(row["path"])
            relation = "writes" if row["writes"] else "reads"
            self.edge(source, _node_id("table", row["tbl"]), relation, row["line"])

    def route_tables(self) -> None:
        """Роут → таблицы через его обработчик: сразу видно, какие данные трогает кнопка/URL."""
        by_symbol: dict[str, set[tuple[str, str]]] = {}
        for edge in list(self.edges.values()):
            if edge["rel"] in {"reads", "writes"}:
                by_symbol.setdefault(edge["from"], set()).add((edge["to"], edge["rel"]))
        for route_id, node in list(self.nodes.items()):
            if node["kind"] != "route":
                continue
            handler = node.get("handler") or ""
            target = self.by_path_name.get((node.get("path", ""), handler)) or self._resolve_symbol(node.get("path", ""), handler)[0]
            for table_id, relation in sorted(by_symbol.get(target or "", ())):
                self.edge(route_id, table_id, relation, node.get("line"), via="handler")

    def env(self) -> None:
        for row in self.conn.execute(
                "SELECT name, default_value, path, line, how, documented FROM env_vars ORDER BY name").fetchall():
            env_id = self.node(_node_id("env", row["name"]), kind="env", label=row["name"],
                               sub="переменная" if row["how"] != "constant" else "константа", path=row["path"],
                               documented=bool(row["documented"]), default=(row["default_value"] or "")[:80])
            source = self._enclosing_symbol(row["path"], row["line"] or 1)
            self.edge(source, env_id, "reads_env", row["line"])

    def _enclosing_symbol(self, path: str, line: int) -> str:
        best: sqlite3.Row | None = None
        for row in self.symbols_by_path.get(path, []):
            if row["kind"] == "module":
                continue
            start, end = row["line"] or 0, row["end_line"] or row["line"] or 0
            if start <= line <= end and (best is None or start > (best["line"] or 0)):
                best = row
        if best is None:
            return _file_id(path)
        return _symbol_id(path, best["qualname"])

    def ui(self) -> None:
        for row in self.conn.execute(
                "SELECT element_id, tag, label, path, line, js_path, js_line, js_symbol, endpoints FROM ui_links").fetchall():
            if not row["element_id"]:
                continue
            ui_id = self.node(_node_id("ui", row["element_id"]), kind="ui", label=(row["label"] or row["element_id"])[:80],
                              sub=row["tag"] or "элемент", path=row["path"], line=row["line"],
                              endpoints=(row["endpoints"] or "")[:200])
            self.edge(_file_id(row["path"]), ui_id, "declares", row["line"])
            if row["js_path"]:
                js_symbol = self.by_path_name.get((row["js_path"], row["js_symbol"] or ""))
                self.edge(ui_id, js_symbol or _file_id(row["js_path"]), "handled_by", row["js_line"])
            for endpoint in _split_list(row["endpoints"]):
                for route_id in self.routes_by_url.get(normalize_url(endpoint), []):
                    self.edge(ui_id, route_id, "calls_api", row["line"])

    def tests(self) -> None:
        for row in self.conn.execute(
                "SELECT test_path, test_name, line, module, symbol FROM test_cover").fetchall():
            source = self.by_path_name.get((row["test_path"], row["test_name"] or "")) or _file_id(row["test_path"])
            target = None
            if row["symbol"]:
                target = self.by_path_name.get((row["module"], row["symbol"]))
            if target is None and row["module"]:
                target = _file_id(row["module"]) if row["module"] in self._known_files() else None
            self.edge(source, target, "covers", row["line"])

    def history(self) -> None:
        unknown: set[str] = set()
        for row in self.conn.execute("SELECT hash, date, author, subject, files FROM history").fetchall():
            commit = self.node(_node_id("commit", (row["hash"] or "")[:8]), kind="commit",
                               label=(row["subject"] or "")[:80], sub=(row["date"] or "")[:10], path="",
                               author=row["author"] or "", date=(row["date"] or "")[:19])
            for path in _split_list(row["files"]):
                if path not in self._known_files():  # удалённые и неиндексируемые файлы не тянем в граф
                    unknown.add(path)
                    continue
                self.edge(commit, _file_id(path), "changed")
        if unknown:
            self.notes.append(f"в истории {len(unknown)} файлов, которых нет в индексе (не показаны)")

    def manifest(self) -> None:
        if not MANIFEST_PATH.is_file():
            self.notes.append("манифеста карты нет: " + str(MANIFEST_PATH))
            return
        try:
            import yaml

            data = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            self.notes.append("манифест не прочитан: " + str(exc))
            return
        id_map: dict[str, str] = {}
        for node in data.get("nodes") or []:
            raw_path = str(node.get("path") or "").split("#", 1)[0]
            target = None
            if raw_path and not any(ch in raw_path for ch in "*{}<>") and raw_path in self._known_files():
                target = _file_id(raw_path)
            if target is None:
                target = self.node(_node_id("manifest", str(node.get("id"))), kind="manifest",
                                   label=str(node.get("title") or node.get("id"))[:100], sub=str(node.get("kind") or ""),
                                   path=raw_path, tags=node.get("tags") or [])
            else:
                self.annotate(target, tags=node.get("tags") or [], manifest_kind=str(node.get("kind") or ""))
            id_map[str(node.get("id"))] = target
        for edge in data.get("edges") or []:
            relation = str(edge.get("relation") or "related")
            self.edge(id_map.get(str(edge.get("from"))), id_map.get(str(edge.get("to"))), f"m:{relation}", None,
                      semantic=1)

    def build(self) -> dict[str, Any]:
        self.files()
        self.symbols()
        self.imports()
        self.calls()
        self.routes()
        self.tables()
        self.table_access()
        self.route_tables()
        self.env()
        self.ui()
        self.tests()
        if self.include_history:
            self.history()
        if self.include_manifest:
            self.manifest()
        meta = {row["key"]: row["value"] for row in self.conn.execute("SELECT key, value FROM meta").fetchall()}
        graph = {
            "version": 1,
            "built_at": int(time.time()),
            "source": {
                "db": "data/project_map.db",
                "manifest": "docs/project-map/manifest.yaml" if self.include_manifest else "",
                "indexed_at": meta.get("indexed_at", ""),
                "git_head": meta.get("git_head", ""),
                "chunker_version": meta.get("chunker_version", ""),
            },
            "nodes": sorted(self.nodes.values(), key=lambda n: n["id"]),
            "edges": sorted(self.edges.values(), key=lambda e: (e["from"], e["to"], e["rel"])),
            "notes": self.notes,
        }
        graph["stats"] = stats(graph)
        return graph


def build(conn: sqlite3.Connection, *, include_external: bool = False, include_history: bool = True,
          include_manifest: bool = True, with_columns: bool = False) -> dict[str, Any]:
    """Собрать полный граф проекта из готового индекса."""
    return _Builder(conn, include_external=include_external, include_history=include_history,
                    include_manifest=include_manifest, with_columns=with_columns).build()


# ───────────────────────────── сводки и виды ─────────────────────────────

def stats(graph: dict[str, Any]) -> dict[str, Any]:
    """Сколько узлов и рёбер каждого вида."""
    by_kind: dict[str, int] = {}
    by_group: dict[str, int] = {}
    for node in graph["nodes"]:
        by_kind[node["kind"]] = by_kind.get(node["kind"], 0) + 1
        if node.get("group"):
            by_group[node["group"]] = by_group.get(node["group"], 0) + 1
    by_rel: dict[str, int] = {}
    for edge in graph["edges"]:
        by_rel[edge["rel"]] = by_rel.get(edge["rel"], 0) + 1
    return {"nodes": len(graph["nodes"]), "edges": len(graph["edges"]),
            "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
            "by_relation": dict(sorted(by_rel.items(), key=lambda kv: -kv[1])),
            "by_group": dict(sorted(by_group.items(), key=lambda kv: -kv[1]))}


def module_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """Свод по файлам: узлы-файлы и рёбра «файл → файл» (вызовы, импорты, тесты, данные, смысл).

    Идентификаторы рёбер — те же, что у узлов (`file:<путь>`), поэтому вид можно фильтровать как любой другой.
    """
    node_file = {n["id"]: n.get("path") for n in graph["nodes"]}
    edges: dict[tuple[str, str, str], dict] = {}
    for edge in graph["edges"]:
        source, target = node_file.get(edge["from"], ""), node_file.get(edge["to"], "")
        if not source or not target or source == target:
            continue
        rel = edge["rel"]
        bucket = ("covers" if rel == "covers" else
                  "imports" if rel.startswith("import") else
                  "calls" if rel.startswith("calls") else
                  "data" if rel in {"reads", "writes"} else
                  "manifest" if rel.startswith("m:") else
                  "history" if rel == "changed" else "other")
        key = (source, target, bucket)
        row = edges.setdefault(key, {"from": _file_id(source), "to": _file_id(target), "rel": bucket, "n": 0})
        row["n"] += edge.get("n", 1)
    nodes = [{"id": _file_id(n["path"]), "label": n["path"].rsplit("/", 1)[-1], "kind": "file",
              "path": n["path"], "sub": n.get("sub", ""), "group": subsystem(n["path"])}
             for n in graph["nodes"] if n["kind"] == "file"]
    return {"nodes": nodes, "edges": sorted(edges.values(), key=lambda e: (e["from"], e["to"], e["rel"])),
            "source": graph.get("source", {})}


def _paths_by_id(modules: dict[str, Any]) -> dict[str, str]:
    return {n["id"]: n["path"] for n in modules.get("nodes") or []}


def modules_summary(graph: dict[str, Any]) -> dict[str, Any]:
    """Компактный машиночитаемый свод для git: подсистемы, связи между ними, файлы со степенями связности."""
    modules = module_graph(graph)
    paths = _paths_by_id(modules)
    summary_stats = graph.get("stats") or stats(graph)
    out_deg: dict[str, int] = {}
    in_deg: dict[str, int] = {}
    subsystem_edges: dict[tuple[str, str, str], int] = {}
    for edge in modules["edges"]:
        source, target = paths.get(edge["from"], ""), paths.get(edge["to"], "")
        out_deg[source] = out_deg.get(source, 0) + edge["n"]
        in_deg[target] = in_deg.get(target, 0) + edge["n"]
        source_group, target_group = subsystem(source), subsystem(target)
        if source_group != target_group:
            key = (source_group, target_group, edge["rel"])
            subsystem_edges[key] = subsystem_edges.get(key, 0) + edge["n"]
    files = [{"path": n["path"], "group": n["group"], "out": out_deg.get(n["path"], 0), "in": in_deg.get(n["path"], 0)}
             for n in modules["nodes"]]
    groups: dict[str, dict[str, int]] = {}
    for item in files:
        row = groups.setdefault(item["group"], {"files": 0, "out": 0, "in": 0})
        row["files"] += 1
        row["out"] += item["out"]
        row["in"] += item["in"]
    return {
        "version": 1,
        "source": graph.get("source") or {},
        "built_at": graph.get("built_at"),
        "stats": summary_stats,
        "subsystems": [{"name": name, **row} for name, row in sorted(groups.items(), key=lambda kv: -kv[1]["files"])],
        "edges": [{"from": source, "to": target, "rel": rel, "n": count}
                  for (source, target, rel), count in sorted(subsystem_edges.items(), key=lambda kv: -kv[1])],
        "files": sorted(files, key=lambda f: (-(f["out"] + f["in"]), f["path"])),
    }


def node_index(graph: dict[str, Any]) -> dict[str, dict]:
    return {n["id"]: n for n in graph["nodes"]}


def find_node(graph: dict[str, Any], query: str) -> dict | None:
    """Найти узел по id, пути, имени функции, роуту или подписи UI (для --graph и страницы)."""
    query = (query or "").strip()
    if not query:
        return None
    nodes = graph["nodes"]
    if any(n["id"] == query for n in nodes):
        return next(n for n in nodes if n["id"] == query)
    lowered = query.lower()
    exact = [n for n in nodes if n["label"].lower() == lowered or n.get("path", "") == query]
    if exact:
        return _best(exact)
    partial = [n for n in nodes if lowered in n["label"].lower() or lowered in n.get("path", "").lower()]
    return _best(partial) if partial else None


def _best(candidates: list[dict]) -> dict:
    """Из совпадений выбираем самое «полезное»: символ/роут выше файла, короткий путь выше длинного."""
    order = {"symbol": 0, "route": 1, "ui": 2, "table": 3, "env": 4, "file": 5, "manifest": 6,
             "commit": 7, "column": 8, "external": 9}
    return sorted(candidates, key=lambda n: (order.get(n["kind"], 9), len(n.get("path", "")), n["id"]))[0]


def neighbors(graph: dict[str, Any], focus: str, *, depth: int = 1, direction: str = "both",
              kinds: Iterable[str] | None = None, relations: Iterable[str] | None = None,
              limit: int = 400) -> dict[str, Any]:
    """Окрестность узла: кто на него влияет и на кого влияет он (по слоям и глубине)."""
    center = find_node(graph, focus)
    if center is None:
        return {"center": None, "nodes": [], "edges": [], "truncated": False, "notes": ["узел не найден"]}
    allowed_kinds = set(kinds) if kinds else None
    allowed_rel = set(relations) if relations else None
    adjacency: dict[str, list[dict]] = {}
    for edge in graph["edges"]:
        if allowed_rel and edge["rel"] not in allowed_rel:
            continue
        adjacency.setdefault(edge["from"], []).append(edge)
        adjacency.setdefault(edge["to"], []).append(edge)
    index = node_index(graph)
    seen = {center["id"]}
    frontier = {center["id"]}
    chosen: dict[tuple[str, str, str], dict] = {}
    for _ in range(max(1, min(depth, 4))):
        next_frontier: set[str] = set()
        for node_id in frontier:
            for edge in adjacency.get(node_id, []):
                if direction == "out" and edge["from"] != node_id:
                    continue
                if direction == "in" and edge["to"] != node_id:
                    continue
                other = edge["to"] if edge["from"] == node_id else edge["from"]
                node = index.get(other)
                if node is None or (allowed_kinds and node["kind"] not in allowed_kinds):
                    continue
                chosen[(edge["from"], edge["to"], edge["rel"])] = edge
                if other not in seen:
                    seen.add(other)
                    next_frontier.add(other)
        frontier = next_frontier
        if not frontier:
            break
    truncated = len(seen) > limit
    chosen_edges = [e for e in chosen.values() if e["from"] in seen and e["to"] in seen]
    if truncated:
        degree: dict[str, int] = {}
        for edge in chosen_edges:
            degree[edge["from"]] = degree.get(edge["from"], 0) + 1
            degree[edge["to"]] = degree.get(edge["to"], 0) + 1
        keep = {center["id"]}
        keep |= {n for n, _ in sorted(degree.items(), key=lambda kv: -kv[1])[: limit - 1]}
        chosen_edges = [e for e in chosen_edges if e["from"] in keep and e["to"] in keep]
        seen = keep
    return {"center": center, "nodes": [index[n] for n in sorted(seen) if n in index],
            "edges": sorted(chosen_edges, key=lambda e: (e["from"], e["to"], e["rel"])),
            "truncated": truncated, "notes": []}


def orphans(conn: sqlite3.Connection, *, limit: int = DEFAULT_ORPHAN_LIMIT) -> list[dict]:
    """Функции, на которые нигде нет ссылок: ни вызовов, ни тестов, ни роутов, ни декораторов, ни упоминаний в коде.

    Упоминания ищутся только во фрагментах кода (``MENTION_CHUNK_KINDS``): имя, встречающееся в документации,
    находку не отменяет — иначе сгенерированный атлас со списком сирот гасил бы сам себя.
    Честная оговорка: динамическая регистрация (`getattr`, списки) статикой не видна, поэтому список и называется
    «проверить».
    """
    rows = conn.execute(
        """
        SELECT s.name, s.qualname, s.path, s.line, s.kind
        FROM symbols s
        WHERE s.kind IN ('function', 'method', 'async function', 'async method')
          AND s.path NOT LIKE '%/tests/%'
          AND (s.decorators IS NULL OR s.decorators = '')
          AND NOT EXISTS (SELECT 1 FROM calls c WHERE c.callee = s.name)
          AND NOT EXISTS (SELECT 1 FROM test_cover t WHERE t.symbol = s.name)
          AND NOT EXISTS (SELECT 1 FROM routes r WHERE r.handler = s.name AND r.path = s.path)
        ORDER BY s.path, s.line
        """
    ).fetchall()
    candidates = {row["name"]: (row["path"], row["line"] or 0) for row in rows
                  if row["name"] and not (row["name"] or "").startswith("__") and row["name"] not in {"main", "lifespan"}}
    referenced: set[str] = set()
    if candidates:
        pattern = re.compile(r"\b(?:" + "|".join(re.escape(name) for name in
                                                sorted(candidates, key=len, reverse=True)) + r")\b")
        marks = ",".join("?" * len(MENTION_CHUNK_KINDS))
        for chunk in conn.execute(f"SELECT path, line_start, line_end, text FROM chunks WHERE kind IN ({marks})",
                                  MENTION_CHUNK_KINDS).fetchall():
            text = chunk["text"] or ""
            if not text:
                continue
            for match in {m.group(0) for m in pattern.finditer(text)}:
                path, line = candidates.get(match, ("", 0))
                own = path == chunk["path"] and (chunk["line_start"] or 0) <= line <= (chunk["line_end"] or 0)
                if not own:  # упоминание вне собственного определения — функция где-то используется
                    referenced.add(match)
    out = [{"name": row["name"], "path": row["path"], "line": row["line"], "kind": row["kind"]}
           for row in rows if row["name"] in candidates and row["name"] not in referenced]
    return out[:limit]


def import_cycles(conn: sqlite3.Connection, *, limit: int = 20) -> list[list[str]]:
    """Циклы импортов между файлами проекта (компоненты сильной связности)."""
    files = {row[0] for row in conn.execute("SELECT path FROM files").fetchall()}
    graph: dict[str, set[str]] = {}
    for row in conn.execute("SELECT path, module, names FROM imports").fetchall():
        for target in resolve_import(row["path"], row["module"] or "", row["names"], files):
            graph.setdefault(row["path"], set()).add(target)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def strongconnect(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        for neighbour in sorted(graph.get(node, ())):
            if neighbour not in index:
                strongconnect(neighbour)
                low[node] = min(low[node], low[neighbour])
            elif neighbour in on_stack:
                low[node] = min(low[node], index[neighbour])
        if low[node] == index[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1 or (len(component) == 1 and component[0] in graph.get(component[0], ())):
                components.append(sorted(component))

    for node in sorted(graph):
        if node not in index:
            strongconnect(node)
    return sorted(components, key=lambda c: (-len(c), c))[:limit]


# ───────────────────────────── выгрузка ─────────────────────────────

def to_json(graph: dict[str, Any], *, indent: int | None = None) -> str:
    return json.dumps(graph, ensure_ascii=False, indent=indent, sort_keys=False)


def to_graphml(graph: dict[str, Any]) -> str:
    """GraphML для Gephi/yEd: атрибуты узлов и рёбер — плоские строки."""
    index = node_index(graph)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
           '<key id="kind" for="node" attr.name="kind" attr.type="string"/>',
           '<key id="sub" for="node" attr.name="sub" attr.type="string"/>',
           '<key id="group" for="node" attr.name="group" attr.type="string"/>',
           '<key id="path" for="node" attr.name="path" attr.type="string"/>',
           '<key id="line" for="node" attr.name="line" attr.type="int"/>',
           '<key id="rel" for="edge" attr.name="relation" attr.type="string"/>',
           '<key id="n" for="edge" attr.name="count" attr.type="int"/>',
           '<key id="eline" for="edge" attr.name="line" attr.type="int"/>',
           '<graph edgedefault="directed">']
    for node in graph["nodes"]:
        out.append(f'<node id="{xml_escape(node["id"])}">')
        out.append(f'<data key="kind">{xml_escape(node["kind"])}</data>')
        out.append(f'<data key="sub">{xml_escape(str(node.get("sub", "")))}</data>')
        out.append(f'<data key="group">{xml_escape(str(node.get("group", "")))}</data>')
        out.append(f'<data key="path">{xml_escape(str(node.get("path", "")))}</data>')
        out.append(f'<data key="line">{int(node.get("line") or 0)}</data>')
        out.append("</node>")
    for n, edge in enumerate(graph["edges"]):
        if edge["from"] not in index or edge["to"] not in index:
            continue
        out.append(f'<edge id="e{n}" source="{xml_escape(edge["from"])}" target="{xml_escape(edge["to"])}">')
        out.append(f'<data key="rel">{xml_escape(edge["rel"])}</data>')
        out.append(f'<data key="n">{int(edge.get("n") or 1)}</data>')
        out.append(f'<data key="eline">{int(edge.get("line") or 0)}</data>')
        out.append("</edge>")
    out += ["</graph>", "</graphml>", ""]
    return "\n".join(out)


def to_dot(graph: dict[str, Any], *, modules_only: bool = False) -> str:
    """DOT для Graphviz/dot -Tsvg."""
    if modules_only:
        graph = module_graph(graph)
        label = lambda n: n["label"]  # noqa: E731
    else:
        label = lambda n: f'{n["label"]}\\n{n.get("path", "")}:{n.get("line") or ""}'.strip(":")  # noqa: E731
    out = ["digraph skkkk {", '  rankdir=LR;', '  node [shape=box, fontsize=9, style=rounded];']
    kinds = {"file": "folder", "symbol": "box", "route": "component", "table": "cylinder", "env": "note",
             "ui": "ellipse", "external": "plaintext", "commit": "point", "manifest": "hexagon",
             "column": "box"}
    for node in graph["nodes"]:
        out.append(f'  "{node["id"]}" [label="{label(node)}", shape={kinds.get(node["kind"], "box")}];')
    for edge in graph["edges"]:
        out.append(f'  "{edge["from"]}" -> "{edge["to"]}" [label="{edge["rel"]}", weight={max(1, min(9, edge.get("n", 1)))}];')
    out += ["}", ""]
    return "\n".join(out)
