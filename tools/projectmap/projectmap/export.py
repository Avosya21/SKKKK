"""Выгрузка карты: страница просмотрщика одним файлом (HTML, работает без сети), данные просмотрщика (json), GraphML и DOT."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

VIEWER_DIR = Path(__file__).parent / "viewer"
FORMATS = ("html", "json", "graphml", "dot")


def render_html(view: dict[str, Any]) -> str:
    """Страница просмотрщика: шаблон, библиотека G6 и данные карты внутри одного файла."""
    template = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")
    library = (VIEWER_DIR / "vendor" / "g6.min.js").read_text(encoding="utf-8")
    data = json.dumps(view, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = template.replace("__DATA__", "\0DATA\0").replace("__G6__", "\0G6\0")
    page = page.replace("<title>Карта проекта</title>", f"<title>{escape(view.get('title') or 'Карта проекта')}</title>")
    return page.replace("\0G6\0", library).replace("\0DATA\0", data)


def render_graphml(view: dict[str, Any]) -> str:
    keys = [("label", "string"), ("path", "string"), ("group", "string"), ("community", "int"), ("fan_in", "int"), ("fan_out", "int"), ("churn", "int")]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">']
    lines += [f'  <key id="{k}" for="node" attr.name="{k}" attr.type="{t}"/>' for k, t in keys]
    lines += ['  <key id="w" for="edge" attr.name="weight" attr.type="int"/>', '  <graph edgedefault="directed">']
    for node in view["nodes"]:
        lines.append(f"    <node id={quoteattr(node['id'])}>")
        for key, _ in keys:
            value = node.get(key)
            if value is not None:
                lines.append(f'      <data key="{key}">{escape(str(value))}</data>')
        lines.append("    </node>")
    for edge in view["edges"]:
        lines.append(f"    <edge source={quoteattr(edge['s'])} target={quoteattr(edge['t'])}><data key=\"w\">{edge['w']}</data></edge>")
    return "\n".join(lines + ["  </graph>", "</graphml>"]) + "\n"


def render_dot(view: dict[str, Any]) -> str:
    def q(text: str) -> str:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = ["digraph projectmap {", "  rankdir=LR;", "  node [shape=box, fontsize=10];"]
    by_group: dict[str, list[dict[str, Any]]] = {}
    for node in view["nodes"]:
        by_group.setdefault(node["group"], []).append(node)
    for number, (group, nodes) in enumerate(sorted(by_group.items())):
        lines.append(f"  subgraph cluster_{number} {{ label={q(group)};")
        lines += [f"    {q(n['id'])} [label={q(n['label'])}];" for n in nodes]
        lines.append("  }")
    lines += [f"  {q(e['s'])} -> {q(e['t'])} [penwidth={min(4, 1 + e['w'] // 5)}];" for e in view["edges"]]
    return "\n".join(lines + ["}"]) + "\n"


def render(view: dict[str, Any], fmt: str) -> str:
    if fmt == "html":
        return render_html(view)
    if fmt == "json":
        return json.dumps(view, ensure_ascii=False, indent=1)
    if fmt == "graphml":
        return render_graphml(view)
    if fmt == "dot":
        return render_dot(view)
    raise ValueError(f"неизвестный формат: {fmt} (доступны: {', '.join(FORMATS)})")
