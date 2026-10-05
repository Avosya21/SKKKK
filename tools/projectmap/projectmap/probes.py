"""Пробы охвата: независимо от графа находят, что есть в проекте, и сверяют это с графом.

Каждая проба возвращает одну или несколько записей `Coverage`. Пробы общие: они не знают фреймворков, а ищут то, что видно
в любом проекте (файлы, определения функций в скриптах, сервисы compose-файлов, ссылки из документов, связи с тестами).
Пробы, которым нужно знание стека (роуты, таблицы), добавляют адаптеры.
"""

from __future__ import annotations

import re
import subprocess
from typing import Callable

import yaml

from projectmap.context import ProjectContext
from projectmap.coverage import Coverage, Report, make_coverage
from projectmap.paths import is_test_path, unique_script_names

CODE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs", ".rb", ".php", ".cs", ".c", ".cc", ".cpp", ".h", ".swift")
SCRIPT_SUFFIXES = (".py", ".ps1", ".sh", ".bat", ".cmd")
SHELL_SUFFIXES = (".ps1", ".psm1", ".sh", ".bash")
PS_FUNCTION = re.compile(r"(?im)^\s*function\s+([\w-]+)")
SH_FUNCTION = re.compile(r"(?m)^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{")
BACKTICK_PATH = re.compile(r"`([\w.\-]+(?:/[\w.\-]+)+/?)`")
COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


def files_probe(ctx: ProjectContext) -> list[Coverage]:
    present = set(ctx.file_node_ids)
    note = f"осознанно исключено конфигом: {ctx.ignored_count}" if ctx.ignored_count else ""
    return [make_coverage("structure", "files", "Файлы проекта в графе", ctx.considered, present, note)]


def shell_functions_probe(ctx: ProjectContext) -> list[Coverage]:
    expected: list[str] = []
    for path in ctx.considered:
        if not path.endswith(SHELL_SUFFIXES):
            continue
        text = ctx.read(path)
        regex = PS_FUNCTION if path.endswith((".ps1", ".psm1")) else SH_FUNCTION
        expected += [f"{path}::{name}" for name in regex.findall(text)]
    present = {f"{n.get('path')}::{n.get('label')}" for n in ctx.nodes_of("symbol")}
    return [make_coverage("structure", "shell_functions", "Функции в скриптах оболочки в графе", expected, present)]


def compose_services_probe(ctx: ProjectContext) -> list[Coverage]:
    expected: list[str] = []
    for path in ctx.considered:
        if path.split("/")[-1] not in COMPOSE_NAMES:
            continue
        try:
            data = yaml.safe_load(ctx.read(path)) or {}
        except yaml.YAMLError:
            continue
        expected += [f"{path}::{name}" for name in (data.get("services") or {})]
    present = {f"{n.get('path')}::{n.get('label')}" for n in ctx.nodes_of("service")}
    return [make_coverage("environment", "compose_services", "Сервисы compose-файлов в графе", expected, present)]


def script_refs_probe(ctx: ProjectContext) -> list[Coverage]:
    """Упоминания одним файлом кода или скрипта другого скрипта по имени (`run_x.py`) и есть ли такая связь в графе."""
    scripts = unique_script_names(ctx.considered)
    names = list(scripts)
    pairs: list[str] = []
    present: set[str] = set()
    for path in ctx.considered:
        if not path.endswith(SCRIPT_SUFFIXES) or is_test_path(path):
            continue
        text = ctx.read(path)
        for name in names:
            target = scripts[name]
            if target == path or not re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])", text):
                continue
            label = f"{path} → {target}"
            pairs.append(label)
            a, b = ctx.file_node_ids.get(path), ctx.file_node_ids.get(target)
            if a and b and (a, b) in ctx.file_links:
                present.add(label)
    return [make_coverage("environment", "script_refs", "Запуски «скрипт → скрипт» в графе", pairs, present)]


def _doc_refs(ctx: ProjectContext) -> tuple[list[tuple[str, str]], set[str]]:
    top_dirs = {p.split("/")[0] for p in ctx.tracked if "/" in p}
    tracked = set(ctx.tracked)
    refs: list[tuple[str, str]] = []
    for path in ctx.considered:
        if not path.endswith(".md"):
            continue
        for token in dict.fromkeys(BACKTICK_PATH.findall(ctx.read(path))):
            if token.split("/")[0] in top_dirs:
                refs.append((path, token.rstrip("/")))
    exists = {t for _, t in refs if t in tracked or (ctx.root / t).exists() or any(p.startswith(t + "/") for p in tracked)}
    return refs, exists


def doc_refs_probe(ctx: ProjectContext) -> list[Coverage]:
    refs, exists = _doc_refs(ctx)
    labels = [f"{doc} → {target}" for doc, target in refs]
    valid = {f"{doc} → {target}" for doc, target in refs if target in exists}
    in_graph: set[str] = set()
    for doc, target in refs:
        a, b = ctx.file_node_ids.get(doc), ctx.file_node_ids.get(target)
        if a and b and (a, b) in ctx.file_links:
            in_graph.add(f"{doc} → {target}")
    files = set(ctx.tracked)
    existing = [f"{doc} → {target}" for doc, target in refs if target in files]
    return [
        make_coverage("meaning", "doc_refs_valid", "Ссылки документов на файлы ведут на существующее", labels, valid,
                      "пропуск = битая ссылка"),
        make_coverage("meaning", "doc_refs_in_graph", "Ссылки документ → файл есть в графе", existing, in_graph),
    ]


INTERACTIVE_TAGS = frozenset({"button", "a", "input", "select", "textarea", "form", "summary"})


def ui_api_probe(ctx: ProjectContext) -> list[Coverage]:
    """Интерактивные элементы (кнопка, ссылка, поле, форма) должны вести к обработчику или к API; контейнеры и подписи — нет."""
    expected = [n["id"] for n in ctx.nodes_of("ui") if n.get("sub") in INTERACTIVE_TAGS]
    api = {"handled_by", "handles", "calls_api"}
    present = {i for i in expected if ctx.touched_by.get(i, set()) & api}
    return [make_coverage("interfaces", "ui_api", "Интерактивные элементы связаны с обработчиком или API", expected, present)]


def data_access_probe(ctx: ProjectContext) -> list[Coverage]:
    expected = [n["id"] for n in ctx.nodes_of("table")]
    present = {i for i in expected if ctx.touched_by.get(i, set()) & {"reads", "writes"}}
    return [make_coverage("data", "data_access", "Таблицы с известным читателем или писателем", expected, present)]


def env_probe(ctx: ProjectContext) -> list[Coverage]:
    """Две стороны одного вопроса: каждая читаемая кодом переменная описана в образце (`.env.example`, compose), и каждая описанная читается."""
    envs = [n for n in ctx.nodes_of("env") if not ctx.config.is_env_ignored(n.get("label") or str(n["id"]).split(":", 1)[-1])]
    read = [n["id"] for n in envs if "reads_env" in ctx.touched_by.get(n["id"], set())]
    declared = {n["id"] for n in envs if n.get("declared") or "declares" in ctx.touched_by.get(n["id"], set())}
    declared_list = sorted(declared)
    read_set = set(read)
    return [make_coverage("settings", "env", "Читаемые кодом переменные окружения описаны в образце", read, declared),
            make_coverage("settings", "env_used", "Описанные в образце переменные читаются кодом", declared_list, read_set,
                          "пропуск = переменная описана, но код её не читает (или читает другой язык вне разбора)")]


def tests_probe(ctx: ProjectContext) -> list[Coverage]:
    covered_symbols = {e["to"] for e in ctx.edges if e.get("rel") == "covers"}
    files_with_tests = {ctx.file_of(s) for s in covered_symbols}
    files_with_symbols = set(ctx.file_of_symbol.values())  # файл без определений (пустой __init__) покрывать нечем
    expected = [p for p in ctx.considered if p.endswith(CODE_SUFFIXES) and not is_test_path(p)
                and ctx.file_node_ids.get(p) in files_with_symbols]
    present = {p for p in expected if ctx.file_node_ids[p] in files_with_tests}
    return [make_coverage("tests", "tests", "Файлы кода с определениями, которые покрывает хотя бы один тест", expected, present)]


def history_probe(ctx: ProjectContext) -> list[Coverage]:
    try:
        out = subprocess.run(["git", "-C", str(ctx.root), "log", "--format=%H"], capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    expected = [h[:8] for h in out.decode().split() if h]
    present = {str(n["id"]).split(":", 1)[-1][:8] for n in ctx.nodes_of("commit")}
    return [make_coverage("history", "history", "Коммиты проекта в графе", expected, present,
                          "коммиты вне разобранных файлов в граф не попадают")]


def metrics_probe(ctx: ProjectContext) -> list[Coverage]:
    # тестовые файлы в граф групп и степеней осознанно не входят: они покрывают код, а не участвуют в его структуре
    expected = [n["path"] for n in ctx.nodes_of("file") if n.get("path") and not is_test_path(n["path"])]
    metric_fields = ("community", "fan_in", "fan_out")
    present = {n["path"] for n in ctx.nodes_of("file") if n.get("path") and any(f in n for f in metric_fields)}
    return [make_coverage("metrics", "metrics", "Файлы кода с метриками (fan-in/out, группа)", expected, present)]


PROBES: dict[str, Callable[[ProjectContext], list[Coverage]]] = {
    "files": files_probe,
    "shell_functions": shell_functions_probe,
    "compose_services": compose_services_probe,
    "script_refs": script_refs_probe,
    "doc_refs": doc_refs_probe,
    "ui_api": ui_api_probe,
    "data_access": data_access_probe,
    "env": env_probe,
    "tests": tests_probe,
    "history": history_probe,
    "metrics": metrics_probe,
}


def run_probes(ctx: ProjectContext, only: list[str] | None = None) -> Report:
    """Выполнить пробы и проставить цели из конфига (по идентификатору записи)."""
    coverages: list[Coverage] = []
    for name, probe in PROBES.items():
        if only and name not in only:
            continue
        for item in probe(ctx):
            item.target = ctx.config.targets.get(item.probe)
            coverages.append(item)
    return Report(coverages)
