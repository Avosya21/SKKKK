"""Командная строка projectmap: `projectmap <команда>`.

Пока реализована только `version`; остальные команды объявлены, чтобы интерфейс был виден с первого дня, и
отвечают, на каком этапе плана они появятся (код выхода 2).
"""

from __future__ import annotations

import argparse
import sys

from projectmap import __version__

# команда -> (что делает, этап плана, на котором появится)
COMMANDS: dict[str, tuple[str, int]] = {
    "init": ("определить стек проекта, создать конфиг и заготовку карты", 5),
    "index": ("разобрать проект и построить граф", 2),
    "graph": ("пересобрать граф, метрики и атлас", 2),
    "check": ("сверить карту с проектом и показать охват по слоям", 1),
    "export": ("выгрузить граф (JSON, GraphML, DOT, HTML)", 4),
    "serve": ("открыть просмотрщик карты", 4),
    "hooks": ("поставить или снять git-хуки проверки карты", 5),
    "mcp": ("запустить MCP-сервер карты для ИИ-агентов", 5),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="projectmap", description="Карта любого проекта: граф, метрики, проверка полноты.")
    sub = parser.add_subparsers(dest="command", metavar="команда")
    sub.add_parser("version", help="показать версию")
    for name, (about, stage) in COMMANDS.items():
        sub.add_parser(name, help=f"{about} (этап {stage} плана, пока не реализовано)")
    return parser


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
    if args.command in COMMANDS:
        about, stage = COMMANDS[args.command]
        print(f"projectmap {args.command}: {about} — появится на этапе {stage} плана, пока не реализовано.", file=sys.stderr)
        return 2
    build_parser().print_help()
    return 0
