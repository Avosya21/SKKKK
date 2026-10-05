"""Командная строка projectmap: `projectmap <команда>`.

Реализованы все команды: `version`, `init`, `index`, `graph`, `check`, `export`, `serve`, `map`, `hooks`, `mcp` (и служебная `hook` для git); остальные команды объявлены, чтобы интерфейс был виден с первого дня, и отвечают,
на каком этапе плана они появятся (код выхода 2).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from projectmap import __version__
from projectmap.atlas import render_atlas
from projectmap.build import build_graph, graph_stats
from projectmap.config import Config, load_config
from projectmap.context import ProjectContext, load_graph
from projectmap.coverage import render_markdown, render_text
from projectmap.export import FORMATS, render
from projectmap.metrics import MetricsOptions, enrich
from projectmap.probes import PROBES, run_probes
from projectmap.schema import validate_graph
from projectmap.view import build_view

# команда -> (что делает, этап плана, на котором появится); все команды реализованы, словарь оставлен для будущих
PLANNED: dict[str, tuple[str, int]] = {}
COMMANDS = PLANNED  # совместимость: объявленные, но ещё не реализованные команды


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="корень проекта (по умолчанию текущий каталог)")
    parser.add_argument("--config", default="", help="конфиг (по умолчанию .projectmap/config.yaml)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="projectmap", description="Карта любого проекта: граф, метрики, проверка полноты.")
    sub = parser.add_subparsers(dest="command", metavar="команда")
    sub.add_parser("version", help="показать версию")
    for name, about in (("index", "разобрать проект и построить граф с метриками"),
                        ("graph", "то же, что index, плюс атлас графа (Markdown) рядом с графом")):
        command = sub.add_parser(name, help=about)
        _common(command)
        command.add_argument("--output", default="", help="куда записать граф (json); по умолчанию из конфига")
        command.add_argument("--no-metrics", action="store_true", help="без метрик (группы, fan-in/out, правки)")
        command.add_argument("--history-limit", type=int, default=500, help="сколько последних коммитов читать из git")
    check = sub.add_parser("check", help="охват карты по слоям: что есть в проекте и сколько из этого в графе")
    _common(check)
    check.add_argument("--graph", default="", help="граф (json); по умолчанию из конфига")
    check.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    check.add_argument("--output", default="", help="записать отчёт в файл вместо вывода на экран")
    check.add_argument("--only", action="append", default=[], choices=sorted(PROBES), help="только эти пробы")
    check.add_argument("--strict", action="store_true", help="код выхода 1, если охват ниже цели")
    init = sub.add_parser("init", help="определить стек проекта, создать .projectmap/config.yaml и правила .gitignore")
    _common(init)
    init.add_argument("--force", action="store_true", help="перезаписать существующий конфиг")
    init.add_argument("--hooks", action="store_true", help="сразу поставить git-хуки")
    scaffold = sub.add_parser("map", help="создать или обновить документ карты (Markdown) по графу: роуты, таблицы, настройки, модули")
    _common(scaffold)
    scaffold.add_argument("--graph", default="", help="готовый граф (json); по умолчанию из конфига, а если его нет — строится заново")
    scaffold.add_argument("--output", default="", help="файл карты; по умолчанию первый из `map.files` конфига или docs/PROJECT-MAP.md")
    scaffold.add_argument("--check", action="store_true", help="ничего не писать; код выхода 1, если документ отстаёт от графа")
    hooks = sub.add_parser("hooks", help="git-хуки: личные данные в коммите и карта вместе с кодом")
    _common(hooks)
    hooks.add_argument("action", choices=["install", "uninstall", "status"])
    hooks.add_argument("--force", action="store_true", help="заменить чужой хук (он сохранится и будет выполняться после нашего)")
    hook = sub.add_parser("hook", help="служебная: вызов проверок из git-хуков")
    _common(hook)
    hook.add_argument("name")
    hook.add_argument("hook_args", nargs="*")
    mcp = sub.add_parser("mcp", help="MCP-сервер карты для ИИ-агентов (stdio, только чтение)")
    _common(mcp)
    mcp.add_argument("--graph", default="", help="граф (json); по умолчанию из конфига")
    mcp.add_argument("--print-config", action="store_true", help="напечатать фрагмент .mcp.json для подключения и выйти")
    export = sub.add_parser("export", help="выгрузить карту: HTML-просмотрщик одним файлом, json, GraphML, DOT")
    _common(export)
    export.add_argument("--format", choices=FORMATS, default="html")
    export.add_argument("--graph", default="", help="готовый граф (json); по умолчанию из конфига, а если его нет — строится заново")
    export.add_argument("--output", default="", help="файл результата; по умолчанию .projectmap/out/map.<формат>")
    serve = sub.add_parser("serve", help="собрать просмотрщик и открыть его в браузере по адресу на этом компьютере")
    _common(serve)
    serve.add_argument("--graph", default="", help="готовый граф (json); по умолчанию из конфига, а если его нет — строится заново")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-open", action="store_true", help="не открывать браузер")
    for name, (about, stage) in PLANNED.items():
        sub.add_parser(name, help=f"{about} (этап {stage} плана, пока не реализовано)")
    return parser


def _load(args: argparse.Namespace) -> tuple[Path, Config]:
    root = Path(args.root).resolve()
    return root, load_config(root, Path(args.config) if args.config else None)


def build_and_write(root: Path, config: Config, output: Path, *, metrics: bool = True, history_limit: int = 500) -> dict:
    """Построить граф (с метриками) и записать его в файл; возвращает граф."""
    graph = build_graph(root, config, history_limit=history_limit)
    if metrics:
        enrich(graph, MetricsOptions(**config.metrics))
        graph["stats"] = graph_stats(graph)  # метрики добавили рёбра co_changes
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8", newline="")
    return graph


def run_index(args: argparse.Namespace, *, atlas: bool) -> int:
    root, config = _load(args)
    output = Path(args.output) if args.output else root / config.graph
    started = time.time()
    graph = build_and_write(root, config, output, metrics=not args.no_metrics, history_limit=args.history_limit)
    stats = graph["stats"]
    print(f"узлов: {stats['nodes']}  рёбер: {stats['edges']}  за {time.time() - started:.1f} с")
    print("узлы: " + ", ".join(f"{kind} {count}" for kind, count in stats["by_kind"].items()))
    for note in graph["notes"]:
        print("  " + note)
    if graph.get("metrics"):
        metrics = graph["metrics"]
        print(f"метрики: групп {len(metrics['communities'])}, модулярность {metrics['modularity']}, "
              f"согласие с подсистемами {metrics['agreement_with_groups']}, хабов {len(metrics['hubs'])}")
    problems = [p for p in validate_graph(graph) if p.severity == "error"]
    for problem in problems[:5]:
        print(f"! граф: {problem.message}", file=sys.stderr)
    print(f"записано: {output}")
    if atlas:
        atlas_path = output.with_name("atlas.md")
        atlas_path.write_text(render_atlas(graph), encoding="utf-8", newline="")
        print(f"записано: {atlas_path}")
    return 1 if problems else 0


def run_map_command(args: argparse.Namespace) -> int:
    from projectmap.scaffold import render_document

    root, config = _load(args)
    graph_path = Path(args.graph) if args.graph else root / config.graph
    graph = load_graph(graph_path) if graph_path.is_file() else build_and_write(root, config, graph_path)
    output = Path(args.output) if args.output else root / (config.map_files[0] if config.map_files else "docs/PROJECT-MAP.md")
    existing = output.read_text(encoding="utf-8") if output.is_file() else None
    text, report = render_document(graph, config, existing, title=config.title or root.name)
    summary = (f"строк {report['rows']}, без вписанного описания {report['unfinished']}"
               + (f", добавлено {report['added']}, удалено {report['removed']}" if existing else ""))
    if args.check:
        stale = existing != text
        print(("карта отстаёт от графа: " if stale else "карта актуальна: ") + summary)
        return 1 if stale else 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8", newline="")
    print(f"записано: {output} ({summary})")
    return 0


def run_init_command(args: argparse.Namespace) -> int:
    from projectmap.hooks import install
    from projectmap.init import run_init

    root = Path(args.root).resolve()
    code, messages = run_init(root, force=args.force)
    for message in messages:
        print(message)
    if code == 0 and args.hooks:
        for name, state in install(root).items():
            print(f"хук {name}: {state}")
    if code == 0:
        print("дальше: projectmap index → projectmap check → projectmap export (или projectmap serve)")
    return code


def run_hooks_command(args: argparse.Namespace) -> int:
    from projectmap import hooks

    root = Path(args.root).resolve()
    try:
        result = {"install": lambda: hooks.install(root, force=args.force), "uninstall": lambda: hooks.uninstall(root), "status": lambda: hooks.status(root)}[args.action]()
    except RuntimeError as exc:
        print(f"projectmap hooks: {exc}", file=sys.stderr)
        return 2
    for name, state in result.items():
        print(f"{name}: {state}")
    return 1 if any(str(s).startswith("skipped") for s in result.values()) else 0


def run_mcp_command(args: argparse.Namespace) -> int:
    from projectmap import mcp_server

    root, config = _load(args)
    if args.print_config:
        print(json.dumps(mcp_server.client_config(root), ensure_ascii=False, indent=2))
        return 0
    return mcp_server.serve(root, Path(args.graph) if args.graph else root / config.graph)


def _view_of(args: argparse.Namespace) -> tuple[Path, dict]:
    """Данные просмотрщика по готовому графу (или по свежему, если графа ещё нет)."""
    root, config = _load(args)
    graph_path = Path(args.graph) if args.graph else root / config.graph
    graph = load_graph(graph_path) if graph_path.is_file() else build_and_write(root, config, graph_path)
    return root, build_view(graph, title=config.title or root.name)


def run_export(args: argparse.Namespace) -> int:
    root, view = _view_of(args)
    output = Path(args.output) if args.output else root / ".projectmap" / "out" / f"map.{args.format}"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(view, args.format), encoding="utf-8", newline="")
    print(f"записано: {output} ({output.stat().st_size // 1024} КБ)")
    return 0


def run_serve(args: argparse.Namespace) -> int:
    import functools
    import http.server
    import webbrowser

    root, view = _view_of(args)
    output = root / ".projectmap" / "out" / "map.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(view, "html"), encoding="utf-8", newline="")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(output.parent))
    handler.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
    with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
        url = f"http://127.0.0.1:{args.port}/map.html"
        print(f"карта: {url} (остановить — Ctrl+C)")
        if not args.no_open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def run_check(args: argparse.Namespace) -> int:
    root, config = _load(args)
    graph_path = Path(args.graph) if args.graph else root / config.graph
    if not graph_path.is_file():
        print(f"projectmap check: графа нет: {graph_path} (постройте его командой `projectmap index`)", file=sys.stderr)
        return 2
    graph = load_graph(graph_path)
    errors = [p for p in validate_graph(graph) if p.severity == "error"]
    for problem in errors[:10]:
        print(f"! граф: {problem.message}", file=sys.stderr)
    report = run_probes(ProjectContext(root, graph, config), args.only or None)
    if args.format == "json":
        text = json.dumps(report.to_dict(), ensure_ascii=False, indent=1)
    elif args.format == "markdown":
        text = render_markdown(report)
    else:
        text = render_text(report)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8", newline="")
    else:
        print(text)
    return 1 if args.strict and (report.gaps() or errors) else 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(f"projectmap {__version__}")
        return 0
    if args.command in ("index", "graph"):
        return run_index(args, atlas=args.command == "graph")
    if args.command == "check":
        return run_check(args)
    if args.command == "init":
        return run_init_command(args)
    if args.command == "map":
        return run_map_command(args)
    if args.command == "hooks":
        return run_hooks_command(args)
    if args.command == "hook":
        from projectmap.hooks import run_hook

        return run_hook(args.name, Path(args.root).resolve(), args.hook_args)
    if args.command == "mcp":
        return run_mcp_command(args)
    if args.command == "export":
        return run_export(args)
    if args.command == "serve":
        return run_serve(args)
    if args.command in PLANNED:
        about, stage = PLANNED[args.command]
        print(f"projectmap {args.command}: {about} — появится на этапе {stage} плана, пока не реализовано.", file=sys.stderr)
        return 2
    build_parser().print_help()
    return 0
