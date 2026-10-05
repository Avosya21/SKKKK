"""Командная строка projectmap: `projectmap <команда>`.

Реализованы `version` и `check`; остальные команды объявлены, чтобы интерфейс был виден с первого дня, и отвечают,
на каком этапе плана они появятся (код выхода 2).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from projectmap import __version__
from projectmap.config import load_config
from projectmap.context import ProjectContext, load_graph
from projectmap.coverage import render_markdown, render_text
from projectmap.probes import PROBES, run_probes
from projectmap.schema import validate_graph

# команда -> (что делает, этап плана, на котором появится)
PLANNED: dict[str, tuple[str, int]] = {
    "init": ("определить стек проекта, создать конфиг и заготовку карты", 5),
    "index": ("разобрать проект и построить граф", 2),
    "graph": ("пересобрать граф, метрики и атлас", 2),
    "export": ("выгрузить граф (JSON, GraphML, DOT, HTML)", 4),
    "serve": ("открыть просмотрщик карты", 4),
    "hooks": ("поставить или снять git-хуки проверки карты", 5),
    "mcp": ("запустить MCP-сервер карты для ИИ-агентов", 5),
}
COMMANDS = PLANNED  # совместимость: объявленные, но ещё не реализованные команды


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="projectmap", description="Карта любого проекта: граф, метрики, проверка полноты.")
    sub = parser.add_subparsers(dest="command", metavar="команда")
    sub.add_parser("version", help="показать версию")
    check = sub.add_parser("check", help="охват карты по слоям: что есть в проекте и сколько из этого в графе")
    check.add_argument("--root", default=".", help="корень проекта (по умолчанию текущий каталог)")
    check.add_argument("--config", default="", help="конфиг (по умолчанию .projectmap/config.yaml)")
    check.add_argument("--graph", default="", help="граф (json); по умолчанию из конфига")
    check.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    check.add_argument("--output", default="", help="записать отчёт в файл вместо вывода на экран")
    check.add_argument("--only", action="append", default=[], choices=sorted(PROBES), help="только эти пробы")
    check.add_argument("--strict", action="store_true", help="код выхода 1, если охват ниже цели")
    for name, (about, stage) in PLANNED.items():
        sub.add_parser(name, help=f"{about} (этап {stage} плана, пока не реализовано)")
    return parser


def run_check(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    config = load_config(root, Path(args.config) if args.config else None)
    graph_path = Path(args.graph) if args.graph else root / config.graph
    if not graph_path.is_file():
        print(f"projectmap check: графа нет: {graph_path} (построить граф можно командой index, этап 2 плана)", file=sys.stderr)
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
    if args.command == "check":
        return run_check(args)
    if args.command in PLANNED:
        about, stage = PLANNED[args.command]
        print(f"projectmap {args.command}: {about} — появится на этапе {stage} плана, пока не реализовано.", file=sys.stderr)
        return 2
    build_parser().print_help()
    return 0
