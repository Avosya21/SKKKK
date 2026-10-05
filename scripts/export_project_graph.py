"""Сборка графа зависимостей проекта: полный граф, свод по модулям, атлас для человека, выгрузки для Gephi/yEd.

```powershell
venv\\Scripts\\python.exe scripts\\export_project_graph.py                 # граф + свод по модулям + атлас
venv\\Scripts\\python.exe scripts\\export_project_graph.py --stats         # только цифры, ничего не писать
venv\\Scripts\\python.exe scripts\\export_project_graph.py --graphml data\\graph.graphml --dot data\\graph.dot
```

Пишет: `data/project_map_graph.json` (полный граф, в git не хранится), `docs/project-map/graph-modules.json`
(свод «файл → файл»), `docs/project-map/DEPENDENCIES.md` (атлас между маркерами `generated:begin/end atlas`).
Источник — индекс `data/project_map.db` и манифест `docs/project-map/manifest.yaml`; боевая БД проекта не читается.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Граф зависимостей проекта SKKKK")
    ap.add_argument("--graph", default=str(ROOT / "data" / "project_map_graph.json"),
                    help="куда писать полный граф (json)")
    ap.add_argument("--modules-json", default=str(ROOT / "docs" / "project-map" / "graph-modules.json"),
                    help="куда писать свод по модулям (файл → файл)")
    ap.add_argument("--modules-full", action="store_true",
                    help="в свод писать все рёбра «файл → файл» (по умолчанию — подсистемы и степени связности)")
    ap.add_argument("--atlas", default=str(ROOT / "docs" / "project-map" / "DEPENDENCIES.md"),
                    help="куда писать атлас (markdown)")
    ap.add_argument("--graphml", default="", help="дополнительно выгрузить GraphML для Gephi/yEd")
    ap.add_argument("--dot", default="", help="дополнительно выгрузить DOT для Graphviz")
    ap.add_argument("--with-external", action="store_true", help="включить узлы внешних вызовов и модулей")
    ap.add_argument("--with-columns", action="store_true", help="включить столбцы БД отдельными узлами")
    ap.add_argument("--no-history", action="store_true", help="без коммитов и рёбер «коммит → файл»")
    ap.add_argument("--no-manifest", action="store_true", help="без смысловых рёбер манифеста карты")
    ap.add_argument("--no-atlas", action="store_true", help="не пересобирать атлас")
    ap.add_argument("--no-metrics", action="store_true", help="без метрик (группы Louvain, fan-in/out, правки) инструмента projectmap")
    ap.add_argument("--stats", action="store_true", help="только показать цифры")
    ap.add_argument("--json", action="store_true", help="цифры в json")
    args = ap.parse_args()

    from app.app.services.project_index import graph as G
    from app.app.services.project_index import graph_atlas as A
    from app.app.services.project_index import store

    started = time.time()
    conn = store.connect()
    try:
        graph = G.build(conn, include_external=args.with_external, include_history=not args.no_history,
                        include_manifest=not args.no_manifest, with_columns=args.with_columns)
        if not args.no_metrics:
            sys.path.insert(0, str(ROOT / "tools" / "projectmap"))
            from projectmap.config import load_config
            from projectmap.metrics import MetricsOptions, enrich

            enrich(graph, MetricsOptions(**load_config(ROOT).metrics))
            graph["stats"] = G.stats(graph)
        stats = graph["stats"]
        written: list[str] = []
        if not args.stats:
            Path(args.graph).parent.mkdir(parents=True, exist_ok=True)
            Path(args.graph).write_text(G.to_json(graph), encoding="utf-8", newline="")
            written.append(args.graph)
            modules = G.module_graph(graph) if args.modules_full else G.modules_summary(graph)
            Path(args.modules_json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.modules_json).write_text(json.dumps(modules, ensure_ascii=False), encoding="utf-8", newline="")
            written.append(args.modules_json)
            if not args.no_atlas:
                written.append(str(A.write_atlas(A.atlas(graph, conn), Path(args.atlas))))
            if args.graphml:
                Path(args.graphml).write_text(G.to_graphml(graph), encoding="utf-8", newline="")
                written.append(args.graphml)
            if args.dot:
                Path(args.dot).write_text(G.to_dot(graph), encoding="utf-8", newline="")
                written.append(args.dot)
        if args.json:
            print(json.dumps({"stats": stats, "written": written, "seconds": round(time.time() - started, 1)},
                             ensure_ascii=False, indent=1))
            return 0
        print(f'узлов: {stats["nodes"]}  рёбер: {stats["edges"]}  за {time.time() - started:.1f} с')
        print("узлы: " + ", ".join(f"{G.NODE_LABELS.get(k, k)} {v}" for k, v in stats["by_kind"].items()))
        print("связи: " + ", ".join(f"{k} {v}" for k, v in list(stats["by_relation"].items())[:14]))
        if graph.get("metrics"):
            m = graph["metrics"]
            print(f'метрики: групп {len(m["communities"])}, модулярность {m["modularity"]}, согласие с подсистемами {m["agreement_with_groups"]}, '
                  f'хабов {len(m["hubs"])}, пар с совместными правками {m["cochange_pairs"]}')
        if graph.get("notes"):
            print("примечания: " + "; ".join(graph["notes"]))
        for path in written:
            print("записано: " + str(Path(path).relative_to(ROOT) if str(path).startswith(str(ROOT)) else path))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
