"""Запускает три ASR-движка ПАРАЛЛЕЛЬНО, каждый в своём процессе и своём окружении.

Это проверочный прогон этапа 3: три черновика на одном файле + сводка для сравнения.

    python run_all.py --source "D:\\Primary_Analitics\\Test_video\\call.mp4"
    python run_all.py --source ... --only whisper,gigaam
"""

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_engines(only: str | None):
    cfg = json.loads((HERE / "engines.json").read_text(encoding="utf-8"))
    engines = cfg["engines"]
    if only:
        wanted = {n.strip() for n in only.split(",")}
        engines = [e for e in engines if e["name"] in wanted]
    return engines


def prepare(source: Path, work_dir: Path) -> dict:
    print("=== Подготовка аудио ===")
    subprocess.run(
        [sys.executable, str(HERE / "prepare_audio.py"), str(source), "--out-dir", str(work_dir)],
        check=True,
    )
    meta_path = work_dir / f"{source.stem}_meta.json"
    return json.loads(meta_path.read_text(encoding="utf-8"))


def run_engine(engine: dict, meta: dict, out_dir: Path, results: dict, lock: threading.Lock):
    name = engine["name"]
    python = engine["python"]
    if not Path(python).exists():
        with lock:
            results[name] = {"status": "no_env", "error": f"нет интерпретатора: {python}"}
        print(f"[{name}] ПРОПУЩЕН: не создано окружение {python}")
        return

    cmd = [
        python,
        str(HERE / engine["script"]),
        "--audio", meta[engine["audio_key"]],
        "--out-dir", str(out_dir),
    ]
    log_path = out_dir / f"{name}.log"
    started = time.time()
    print(f"[{name}] старт")

    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(HERE))

    elapsed = time.time() - started
    status = "ok" if proc.returncode == 0 else "error"
    with lock:
        results[name] = {
            "status": status,
            "returncode": proc.returncode,
            "wall_sec": round(elapsed, 1),
            "log": str(log_path),
        }
    print(f"[{name}] завершён за {elapsed:.1f} с, код {proc.returncode}")


def summarize(source: Path, meta: dict, out_dir: Path, results: dict, wall_total: float):
    stem = source.stem
    duration = meta.get("duration_sec") or 0

    lines = [
        f"# Сравнение трёх ASR: {source.name}",
        "",
        f"Длительность: {duration / 60:.1f} мин",
        f"Общее время параллельного прогона: {wall_total / 60:.1f} мин",
        "",
        "| Движок | Статус | Время, с | RTF | Символов |",
        "|---|---|---|---|---|",
    ]

    for name, res in results.items():
        json_path = out_dir / f"{stem}_raw_{name}.json"
        chars, rtf = "-", "-"
        if json_path.exists():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            chars = data.get("chars", "-")
            t = data.get("transcribe_sec")
            if t and duration:
                rtf = f"{duration / t:.1f}x"
        lines.append(
            f"| {name} | {res['status']} | {res.get('wall_sec', '-')} | {rtf} | {chars} |"
        )

    lines += ["", "## Первые 800 символов каждого движка", ""]
    for name in results:
        txt_path = out_dir / f"{stem}_raw_{name}.txt"
        lines.append(f"### {name}")
        lines.append("")
        if txt_path.exists():
            lines.append("```")
            lines.append(txt_path.read_text(encoding="utf-8")[:800])
            lines.append("```")
        else:
            lines.append("_нет результата, смотрите лог_")
        lines.append("")

    report = out_dir / f"{stem}_COMPARE.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nСводка: {report}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="видео или аудио файл")
    ap.add_argument("--work-dir", default=str(HERE / "work"))
    ap.add_argument("--out-dir", default=str(HERE / "out"))
    ap.add_argument("--only", default=None, help="например: whisper,tone")
    args = ap.parse_args()

    source = Path(args.source)
    if not source.exists():
        print(f"ОШИБКА: файл не найден: {source}", file=sys.stderr)
        return 2

    work_dir = Path(args.work_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = prepare(source, work_dir)
    engines = load_engines(args.only)

    print(f"\n=== Параллельный запуск: {', '.join(e['name'] for e in engines)} ===")
    results: dict = {}
    lock = threading.Lock()
    threads = [
        threading.Thread(target=run_engine, args=(e, meta, out_dir, results, lock))
        for e in engines
    ]

    started = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall_total = time.time() - started

    summarize(source, meta, out_dir, results, wall_total)

    failed = [n for n, r in results.items() if r["status"] != "ok"]
    if failed:
        print(f"\nНе отработали: {', '.join(failed)}. Смотрите логи в {out_dir}")
        return 1
    print("\nВсе три движка отработали.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
