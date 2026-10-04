"""Проверка дрейфа: карта проекта и манифест против реального кода. Только чтение, если нет --fix.

  venv\\Scripts\\python.exe scripts\\check_map_drift.py          # отчёт, код выхода 1 при расхождениях
  venv\\Scripts\\python.exe scripts\\check_map_drift.py --fix    # перегенерировать реестр в PROJECT-MAP.md и
                                                              # добавить узлы (auto: true) для файлов без узла

Что проверяется:
  1. файлы кода (app/, asr/, scripts/, tools/), которых нет ни в карте, ни в манифесте;
  2. узлы манифеста с несуществующим `path`;
  3. рёбра манифеста, ссылающиеся на несуществующие узлы;
  4. модули без докстринга (services, routers, models, workers, mcp) — предупреждение;
  5. реестр модулей в PROJECT-MAP.md (между маркерами generated) отстаёт от кода;
  6. роуты (с префиксами роутеров), таблицы БД и переменные окружения из кода, не упомянутые в карте
     (без учёта generated-реестра; системные и тестовые переменные — `ENV_IGNORE`).
Реестр между маркерами <!-- generated:begin registry --> / end генерируется; руками его не править.
"""

from __future__ import annotations

import argparse
import ast
import re
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

import yaml  # noqa: E402

MAP = ROOT / "docs" / "PROJECT-MAP.md"
MANIFEST = ROOT / "docs" / "project-map" / "manifest.yaml"
BEGIN = "<!-- generated:begin registry -->"
END = "<!-- generated:end registry -->"

CODE_GLOBS = [
    "app/app/*.py", "app/app/services/*.py", "app/app/routers/*.py", "app/app/models/*.py", "app/app/workers/*.py",
    "app/app/mcp/*.py", "app/app/services/project_index/*.py", "app/app/static/*.js", "app/app/static/*.css", "app/app/templates/*.html",
    "app/app/templates/partials/*.html", "asr/*.py", "asr/lab/*.py", "scripts/*.py", "scripts/*.ps1", "scripts/smoke/*.py",
    "tools/*.py", "tools/livekit/*.py",
]
DOC_DIRS = ["app/app/services", "app/app/routers", "app/app/models", "app/app/workers", "app/app/mcp", "app/app/services/project_index"]


def code_files() -> list[str]:
    out: set[str] = set()
    for pattern in CODE_GLOBS:
        for path in ROOT.glob(pattern):
            if path.is_file() and path.name != "__init__.py" and "__pycache__" not in path.parts:
                out.add(path.relative_to(ROOT).as_posix())
    return sorted(out)


def first_doc_line(rel: str) -> str:
    path = ROOT / rel
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    if rel.endswith(".py"):
        try:
            doc = ast.get_docstring(ast.parse(text)) or ""
        except SyntaxError:
            doc = ""
        line = next((l.strip() for l in doc.splitlines() if l.strip()), "")
        return line[:140]
    if rel.endswith(".ps1"):
        lines = text.splitlines()
        for i, line in enumerate(lines[:40]):
            if line.strip().lower().startswith((".synopsis", ".description")) and i + 1 < len(lines):
                return lines[i + 1].strip()[:140]
        for line in lines[:12]:
            if line.lstrip().startswith("#") and not line.lower().lstrip("# ").startswith(("requires", "region")):
                s = line.strip("#<>. \t")
                if len(s) > 8:
                    return s[:140]
    if rel.endswith((".js", ".css")):
        # только комментарий в шапке файла; первая строка кода описанием не считается
        for line in text.splitlines()[:6]:
            stripped = line.strip()
            if not stripped:
                continue
            if not stripped.startswith(("/*", "//", "*")):
                return ""
            s = stripped.strip("/*# \t")
            if s:
                return s[:140]
    if rel.endswith(".html"):
        # описание шаблона — Jinja-комментарий `{# … #}` в первых строках (в HTML-ответ не попадает)
        m = re.search(r"\{#\s*(.+?)\s*#\}", "\n".join(text.splitlines()[:5]), re.S)
        if m:
            return " ".join(m.group(1).split())[:140]
    return ""


def load_manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}


def registry_text(files: list[str]) -> str:
    groups: dict[str, list[str]] = {}
    for rel in files:
        groups.setdefault(rel.rsplit("/", 1)[0], []).append(rel)
    lines = [BEGIN, "",
             "> Генерируется `scripts/check_map_drift.py --fix` из кода. Не править руками: описание берётся из докстринга "
             "(для модулей без докстринга — прочерк, его стоит добавить). Смысловое описание — в разделах выше.", ""]
    for folder in sorted(groups):
        lines += [f"**`{folder}/`**", "", "| Файл | Назначение |", "|---|---|"]
        for rel in groups[folder]:
            lines.append(f"| `{rel.rsplit('/', 1)[-1]}` | {first_doc_line(rel).replace('|', '/') or '—'} |")
        lines.append("")
    lines.append(END)
    return "\n".join(lines)


def _norm_params(text: str) -> str:
    """Имена параметров пути в карте пишут по-разному (`{id}`, `{project_id}`, `…`) — приводим к `{}`."""
    return re.sub(r"\{[^}]*\}|…", "{}", text)


# Системные и тестовые переменные: карте описывать их незачем (их задаёт ОС, pytest или стенд проверок).
ENV_IGNORE = re.compile(
    r"^(PATH|PATHEXT|HOME|USERPROFILE|TEMP|TMP|APPDATA|LocalAppData|ProgramFiles(\(x86\))?|SESSIONNAME|COMPUTERNAME|"
    r"PYCHARM_HOSTED|PYTEST_CURRENT_TEST|PYTHONPATH|PYTHONIOENCODING|SMOKE_\w*|\{\}\w*|\w*\{\}\w*|"
    # внутренние: конвейер сам подставляет их подпроцессам (`pipeline_audio`, `artifact_audio`) и контейнерам ASR
    r"SKKKK_ARTIFACT_ID|SKKKK_PROJECT_ID|SKKKK_AUDIO_URL|SKKKK_MATCH_JSON|APP_DIR|OUT_DIR|ASR_ENGINE)$"
)


def _code_env_names() -> set[str]:
    """Переменные окружения, которые код читает: поля `Settings` (в верхнем регистре) и os.environ/os.getenv."""
    names: set[str] = set()
    cfg = ROOT / "app" / "app" / "config.py"
    if cfg.is_file():
        for cls in [n for n in ast.parse(cfg.read_text(encoding="utf-8")).body if isinstance(n, ast.ClassDef)]:
            for stmt in cls.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    names.add(stmt.target.id.upper())
    for pattern in ("app/app/*.py", "app/app/services/*.py", "app/app/routers/*.py", "app/app/workers/*.py", "asr/*.py", "scripts/*.py"):
        for path in ROOT.glob(pattern):
            if "/tests/" in path.as_posix():
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                name = None
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
                    if ast.unparse(node.func) in {"os.getenv", "os.environ.get", "environ.get"} and isinstance(node.args[0], ast.Constant):
                        name = node.args[0].value
                elif isinstance(node, ast.Subscript) and ast.unparse(node.value) in {"os.environ", "environ"} and isinstance(node.slice, ast.Constant):
                    name = node.slice.value
                if isinstance(name, str) and name:
                    names.add(name)
    return {n for n in names if not ENV_IGNORE.match(n)}


def coverage(map_text: str) -> dict:
    """Роуты, таблицы БД и переменные окружения из кода, о которых в карте нет ни слова."""
    import sqlite3

    from app.app.services.project_index import analyzers
    from app.app.services.project_index.store import SCHEMA

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    analyzers.analyze_routes(conn)
    analyzers.analyze_db(conn)
    flat = _norm_params(map_text)

    def in_map(url: str) -> bool:
        """Роут упомянут полным адресом или коротко — «…/agent-inbox» после `/projects/{id}` (перед хвостом
        обязательно `…` или `{id}`); граница слова обязательна, чтобы `/people` не засчитывался из-за `/people/{id}/avatar`."""
        norm = _norm_params(url)
        tails = [norm] + (["{}" + norm[len("/projects/{}"):]] if norm.startswith("/projects/{}/") else [])
        return any(re.search(re.escape(t) + r"(?![\w\-/{])", flat) for t in tails)

    routes = sorted({f"{r['method']} {r['url']}" for r in conn.execute("SELECT method, url FROM routes")
                     if r["url"] != "/" and not in_map(r["url"])})
    tables = sorted(r[0] for r in conn.execute("SELECT DISTINCT tbl FROM db_columns") if not re.search(rf"\b{re.escape(r[0])}\b", map_text))
    env = sorted(n for n in _code_env_names() if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])", map_text))
    conn.close()
    return {"routes": routes, "tables": tables, "env": env}


def analyse() -> dict:
    files = code_files()
    map_text = MAP.read_text(encoding="utf-8")
    outside_registry = map_text.split(BEGIN)[0] + (map_text.split(END)[1] if END in map_text else "")
    manifest = load_manifest()
    nodes = manifest.get("nodes") or []
    node_paths = {str(n.get("path") or "").split("#", 1)[0] for n in nodes}
    ids = {n.get("id") for n in nodes}
    missing_in_map = [f for f in files if f.rsplit("/", 1)[-1] not in map_text and f not in node_paths]
    missing_manifest = [f for f in files if f not in node_paths and "/tests/" not in f]
    dead_nodes = []
    for n in nodes:
        raw_path = str(n.get("path") or "")
        clean = raw_path.split("#", 1)[0]  # якорь `#id` — часть адреса, а не имени файла
        if not clean or clean.startswith("/") or any(ch in clean for ch in "*{}<>"):
            continue
        if n.get("kind") in {"data", "docker", "external"}:
            continue
        if not (ROOT / clean).exists():
            dead_nodes.append((n["id"], raw_path))
    bad_edges = [e for e in manifest.get("edges") or [] if e.get("from") not in ids or e.get("to") not in ids]
    no_doc = []
    for folder in DOC_DIRS:
        for path in sorted((ROOT / folder).glob("*.py")):
            if path.name == "__init__.py":
                continue
            rel = path.relative_to(ROOT).as_posix()
            if not first_doc_line(rel):
                no_doc.append(rel)
    # шаблоны и JS тоже должны иметь описание: `{# … #}` / комментарий в шапке
    for pattern in ("app/app/templates/*.html", "app/app/templates/partials/*.html", "app/app/static/*.js"):
        for path in sorted(ROOT.glob(pattern)):
            rel = path.relative_to(ROOT).as_posix()
            if not first_doc_line(rel):
                no_doc.append(rel)
    registry_stale = registry_text(files) not in map_text if BEGIN in map_text else True
    return {"files": files, "missing_in_map": missing_in_map, "missing_manifest": missing_manifest, "dead_nodes": dead_nodes,
            "bad_edges": bad_edges, "no_doc": no_doc, "registry_stale": registry_stale, "outside": outside_registry,
            **{f"missing_{k}": v for k, v in coverage(outside_registry).items()}}


def fix(report: dict) -> None:
    files = report["files"]
    text = MAP.read_text(encoding="utf-8")
    block = registry_text(files)
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        text = head + block + tail
    else:
        marker = "\n---\n\n## 16. Граф зависимостей проекта"
        section = "\n---\n\n## 15. Реестр модулей (генерируется)\n\n" + block + "\n"
        text = text.replace(marker, section + marker, 1) if marker in text else text.rstrip() + "\n" + section
    MAP.write_text(text, encoding="utf-8", newline="")
    # узлы манифеста для файлов без узла
    raw = MANIFEST.read_text(encoding="utf-8")
    manifest = yaml.safe_load(raw) or {}
    ids = {n.get("id") for n in manifest.get("nodes") or []}
    add: list[str] = []
    for rel in report["missing_manifest"]:
        stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("-", "_")
        prefix = ("svc" if "/services/" in rel else "router" if "/routers/" in rel else "model" if "/models/" in rel else
                  "ui" if rel.startswith("app/app/templates") else "static" if rel.startswith("app/app/static") else
                  "asr" if rel.startswith("asr/") else "script" if rel.startswith("scripts/") else "tool" if rel.startswith("tools/") else "mod")
        nid = f"{prefix}_{stem}"
        if nid in ids:
            nid = f"{nid}_auto"
        if nid in ids:
            continue
        ids.add(nid)
        title = first_doc_line(rel) or rel
        add.append(f"  - id: {nid}\n    kind: {'service' if prefix == 'svc' else 'router' if prefix == 'router' else 'model' if prefix == 'model' else 'file'}\n"
                   f"    path: {rel}\n    title: {json.dumps(title, ensure_ascii=False)}\n    tags: [auto]\n    auto: true\n")
    if add:
        head, sep, tail = raw.partition("\nedges:")
        raw = head.rstrip("\n") + "\n\n" + "\n".join(add) + sep + tail
        MANIFEST.write_text(raw, encoding="utf-8", newline="")
    print(f"исправлено: реестр перегенерирован, добавлено узлов манифеста: {len(add)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--strict-docs", action="store_true", help="модули без докстринга считать ошибкой")
    args = ap.parse_args()
    report = analyse()
    if args.fix:
        fix(report)
        report = analyse()
    problems = 0
    def show(title: str, items: list, hard: bool = True) -> None:
        nonlocal problems
        if items:
            print(f"{'✗' if hard else '!'} {title}: {len(items)}")
            for it in items[:25]:
                print("    ", it)
            if len(items) > 25:
                print(f"     … и ещё {len(items) - 25}")
            if hard:
                problems += 1
        else:
            print(f"✓ {title}: нет")
    show("файлы кода, которых нет ни в карте, ни в манифесте", report["missing_in_map"])
    show("узлы манифеста с несуществующим path", report["dead_nodes"])
    show("рёбра манифеста на несуществующие узлы", [f"{e['from']} -> {e['to']} ({e.get('relation')})" for e in report["bad_edges"]])
    show("роуты, которых нет в карте", report["missing_routes"])
    show("таблицы БД, которых нет в карте", report["missing_tables"])
    show("переменные окружения, которых нет в карте", report["missing_env"])
    show("модули без докстринга", report["no_doc"], hard=args.strict_docs)
    if report["registry_stale"]:
        print("✗ реестр модулей в PROJECT-MAP.md отстаёт от кода (запустите с --fix)")
        problems += 1
    else:
        print("✓ реестр модулей актуален")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
