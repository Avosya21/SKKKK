"""Единая команда QA (пункт плана 5.5): одна команда — одна картина «стабильно ли продукт».

    venv\\Scripts\\python scripts\\qa_all.py                 # тесты + смоук + аудит целостности + отчёт здоровья
    venv\\Scripts\\python scripts\\qa_all.py --per-file      # + каждый тест-файл отдельно (долго)
    venv\\Scripts\\python scripts\\qa_all.py --evals         # + eval ТЗ и чата на ПИЛОТЕ (нужна LM Studio, только чтение)

Смоук требует SMOKE_ADMIN_PW / SMOKE_MANAGER_PW в окружении (иначе шаги пропускаются с пометкой SKIP).
Код выхода 1, если любой шаг красный. Отчёт: data/eval/qa_<время>.json.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def run(name: str, cmd: list[str], *, skip_codes=(), timeout: int = 3600) -> dict:
    started = time.time()
    print(f"\n=== {name}", flush=True)
    try:
        r = subprocess.run(cmd, cwd=ROOT, env=ENV, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        code = r.returncode
        tail = "\n".join((r.stdout or "").strip().splitlines()[-6:])
    except subprocess.TimeoutExpired:
        code, tail = 124, "таймаут"
    status = "SKIP" if code in skip_codes else ("OK" if code == 0 else "FAIL")
    print(f"{status} ({time.time() - started:.0f} с)\n{tail}", flush=True)
    return {"step": name, "status": status, "seconds": round(time.time() - started), "tail": tail}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-file", action="store_true")
    ap.add_argument("--evals", action="store_true")
    args = ap.parse_args()
    steps = [run("pytest (весь набор)", [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"])]
    if args.per_file:
        for path in sorted((ROOT / "app" / "app" / "tests").glob("test_*.py")):
            steps.append(run(f"pytest {path.name} отдельно", [PY, "-m", "pytest", str(path), "-n0", "-q", "-p", "no:cacheprovider"]))
    steps.append(run("смоук на живом сервере", [PY, str(ROOT / "scripts" / "smoke" / "run_all.py")], skip_codes=(77,)))
    steps.append(run("аудит целостности данных", [PY, str(ROOT / "scripts" / "audit_integrity.py")]))
    steps.append(run("отчёт о здоровье", [PY, str(ROOT / "scripts" / "health_report.py")]))
    steps.append(run("набор вопросов индекса проекта", [PY, str(ROOT / "scripts" / "eval_index.py"), "--validate"]))
    if args.evals:
        steps.append(run("eval ТЗ (ПИЛОТ, только чтение)", [PY, str(ROOT / "scripts" / "eval_tz.py"), "--project", "18", "--runs", "2",
                                                            "--facts", str(ROOT / "docs" / "eval" / "tz_facts_project18.json")]))
        steps.append(run("eval чата (копия БД)", [PY, str(ROOT / "scripts" / "eval_chat.py"), "--project", "18",
                                                   "--questions", str(ROOT / "docs" / "eval" / "chat_questions_project18.json")]))
        steps.append(run("eval индекса проекта (recall@5)", [PY, str(ROOT / "scripts" / "eval_index.py"), "--min-recall5", "0.85"]))
    out = ROOT / "data" / "eval"
    out.mkdir(parents=True, exist_ok=True)
    report = out / f"qa_{datetime.now():%Y%m%d_%H%M%S}.json"
    report.write_text(json.dumps(steps, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "\n".join(f"{s['status']:5} {s['step']}" for s in steps))
    print("отчёт:", report)
    return 1 if any(s["status"] == "FAIL" for s in steps) else 0


if __name__ == "__main__":
    sys.exit(main())
