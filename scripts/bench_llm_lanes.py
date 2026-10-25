"""Замер этапа 0 плана docs/plans/LLM-LANES-PLAN.md: память и скорость локальной модели в два потока.

Для каждого окна (--ctx) модель загружается с `--parallel 2` по профилю tools/lmstudio/qwen-profile.json, затем:
  idle       — память без запросов;
  chat       — один запрос размера чата (~19k токенов промпта);
  bg         — один запрос размера воркера (~9k);
  chat+bg    — оба одновременно;
  big        — чат 22k + фоновый 30k одновременно (худший случай окна).
Память — пик nvidia-smi раз в 0,5 с. Коэффициент «символы → токены» — по prompt_tokens ответа.
Запуск при пустой очереди воркера:  venv\\Scripts\\python.exe scripts\\bench_llm_lanes.py --ctx 48000 56000
Отчёт: data/eval/llm_lanes_bench_<время>.md и .json. Модель после замера выгружается (--keep — оставить).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import random
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

os.environ.setdefault("PRIMARY_ANALITICS_DISABLE_LLM_SELFCHECK", "1")

from app.app.config import Settings  # noqa: E402
from app.app.services import llm_keeper  # noqa: E402
from app.app.services.llm import _completions_http  # noqa: E402 — без полос: замер сам управляет загрузкой

TEXT_SOURCES = ["docs/PLAN.md", "docs/DECISIONS.md", "docs/TESTING.md", "docs/PROJECT-MAP.md"]


class VramSampler:
    def __init__(self) -> None:
        self.peak = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                raw = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], text=True, timeout=10
                )
                self.peak = max(self.peak, int(raw.split()[0]))
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(0.5)

    def __enter__(self):
        self.peak = 0
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


def corpus() -> str:
    parts = []
    for rel in TEXT_SOURCES:
        path = ROOT / rel
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n\n".join(parts)


def make_prompt(text: str, chars: int) -> str:
    start = random.randrange(0, max(1, len(text) - chars))
    # уникальная метка в начале: иначе кэш префикса LM Studio исказит время второго прогона
    return f"Метка прогона {random.random():.12f}.\n" + text[start : start + chars]


def call(settings: Settings, user: str, max_tokens: int) -> dict:
    started = time.monotonic()
    try:
        text, meta = _completions_http(
            base_url=settings.lmstudio_base_url,
            api_key=settings.lmstudio_api_key,
            model=settings.lmstudio_model,
            system="Кратко перескажи текст пользователя по-русски.",
            user=user,
            temperature=0.0,
            max_tokens=max_tokens,
            timeout=900,
        )
        return {"ok": True, "seconds": round(time.monotonic() - started, 1), **meta, "chars": len(user)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "seconds": round(time.monotonic() - started, 1), "error": str(exc)[:300], "chars": len(user)}


def run_parallel(settings: Settings, jobs: list[tuple[str, str, int]]) -> dict:
    results: dict[str, dict] = {}

    def worker(name: str, prompt: str, max_tokens: int) -> None:
        results[name] = call(settings, prompt, max_tokens)

    threads = [threading.Thread(target=worker, args=job) for job in jobs]
    for i, t in enumerate(threads):
        t.start()
        if i == 0:
            time.sleep(1.0)  # чат приходит, когда фон уже идёт — как в жизни
    for t in threads:
        t.join()
    return results


def queue_busy() -> int:
    con = sqlite3.connect(ROOT / "data" / "app.db")
    try:
        return con.execute("select count(*) from jobs where status in ('queued','running')").fetchone()[0]
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ctx", type=int, nargs="+", default=[48000, 56000])
    ap.add_argument("--chat-tokens", type=int, default=19000)
    ap.add_argument("--bg-tokens", type=int, default=9000)
    ap.add_argument("--out-tokens", type=int, default=400)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--force", action="store_true", help="не проверять очередь воркера")
    args = ap.parse_args()

    if not args.force and queue_busy():
        print("В очереди воркера есть задачи — замер исказится. Повторите при пустой очереди или --force.")
        return 2
    settings = Settings()
    base = llm_keeper.load_profile()
    text = corpus()
    report: list[dict] = []
    ratio = 2.6  # символов на токен; уточняется первым запросом

    for ctx in args.ctx:
        profile = dataclasses.replace(base, context_length=ctx, parallel=2)
        llm_keeper.unload(settings, reason="bench")
        time.sleep(3)
        with VramSampler() as s:
            time.sleep(3)
        empty = s.peak
        with llm_keeper.model_lock(settings):
            entry = llm_keeper._load_locked(settings, profile, reason="bench")
        with VramSampler() as s:
            time.sleep(5)
        row: dict = {"ctx": ctx, "parallel": entry.get("parallel"), "vram_empty": empty, "vram_idle": s.peak, "runs": {}}

        probe = call(settings, make_prompt(text, 20000), 1)
        if probe.get("ok") and probe.get("prompt_tokens"):
            ratio = probe["chars"] / probe["prompt_tokens"]
        row["chars_per_token"] = round(ratio, 3)
        chat_chars = int(args.chat_tokens * ratio)
        bg_chars = int(args.bg_tokens * ratio)

        scenarios = {
            "chat": [("chat", make_prompt(text, chat_chars), args.out_tokens)],
            "bg": [("bg", make_prompt(text, bg_chars), args.out_tokens)],
            "chat+bg": [("bg", make_prompt(text, bg_chars), args.out_tokens), ("chat", make_prompt(text, chat_chars), args.out_tokens)],
            "big": [("bg", make_prompt(text, int(30000 * ratio)), args.out_tokens), ("chat", make_prompt(text, int(22000 * ratio)), args.out_tokens)],
        }
        for name, jobs in scenarios.items():
            with VramSampler() as s:
                res = run_parallel(settings, jobs)
            row["runs"][name] = {"vram_peak": s.peak, **res}
            print(ctx, name, s.peak, json.dumps(res, ensure_ascii=False))
        report.append(row)

    if not args.keep:
        llm_keeper.unload(settings, reason="bench done")

    out_dir = ROOT / "data" / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    (out_dir / f"llm_lanes_bench_{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["| окно | сценарий | пик VRAM, MiB | запрос | промпт, ток | ответ, ток | секунд | ошибка |", "|---|---|---|---|---|---|---|---|"]
    for row in report:
        lines.append(f"| {row['ctx']} | пусто / модель в покое | {row['vram_empty']} / {row['vram_idle']} | | | | | |")
        for name, run in row["runs"].items():
            for key, res in run.items():
                if key == "vram_peak":
                    continue
                lines.append(
                    f"| {row['ctx']} | {name} | {run['vram_peak']} | {key} | {res.get('prompt_tokens')} | "
                    f"{res.get('completion_tokens')} | {res.get('seconds')} | {res.get('error', '')} |"
                )
    (out_dir / f"llm_lanes_bench_{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
