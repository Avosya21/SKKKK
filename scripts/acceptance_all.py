"""Полный acceptance на новой машине: материалы (GPU) + smoke/qa + pytest + health + evals.

Цель — найти ошибки. Не поднимающиеся компоненты и падения шагов = FAIL (не маскируем KNOWN).
Перед прогоном сами поднимаем то, что нужно: GigaAM, Playwright-браузеры; Android — с ``--boot``.

    venv\\Scripts\\python scripts\\acceptance_all.py
    venv\\Scripts\\python scripts\\acceptance_all.py --materials-only --keep-materials

Нужны: живой портал/воркер/LiveKit; вход через SMOKE_OTP_CODE / SMOKE_PERSONAL_PW
или ``E2E_ALLOW_MINT=1`` + mint_smoke_token. Песочница 29.
Отчёт: data/eval/acceptance_YYYYMMDD_HHMMSS.json
Любой FAIL → exit 1. Код 77 шага тоже FAIL (нет устройства/компонента — это дефект стенда).
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


def run(name: str, cmd: list[str], *, timeout: int = 10_800) -> dict:
    started = time.time()
    print(f"\n=== {name}", flush=True)
    try:
        r = subprocess.run(
            cmd,
            cwd=ROOT,
            env=ENV,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        code = r.returncode
        out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
        tail = "\n".join(out.splitlines()[-12:])
    except subprocess.TimeoutExpired:
        code, tail = 124, "таймаут"
    # 77 раньше значил «SKIP нет устройства» — для acceptance это FAIL: стенд обязан быть полным
    if code == 0:
        status = "OK"
    else:
        status = "FAIL"
    print(f"{status} ({time.time() - started:.0f} с)\n{tail}", flush=True)
    return {
        "step": name,
        "status": status,
        "seconds": round(time.time() - started),
        "code": code,
        "tail": tail,
    }


def ensure_gigaam() -> None:
    """Поднятие Docker GigaAM, если контейнер не Up."""
    try:
        ps = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        print(f"WARN docker: {exc}", flush=True)
        return
    up = any("primaryanalitics-gigaam" in line and "Up" in line for line in (ps.stdout or "").splitlines())
    if up:
        print("GigaAM уже Up", flush=True)
        return
    print("поднимаю primaryanalitics-gigaam…", flush=True)
    compose = ROOT / "asr" / "docker-compose.yml"
    if compose.is_file():
        subprocess.run(
            ["docker", "compose", "-f", str(compose), "up", "-d"],
            cwd=ROOT,
            timeout=300,
            check=False,
        )
    else:
        subprocess.run(["docker", "start", "primaryanalitics-gigaam"], timeout=120, check=False)
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            import httpx

            r = httpx.get("http://127.0.0.1:18102/", timeout=3)
            if r.status_code < 500:
                print("GigaAM HTTP готов", flush=True)
                return
        except Exception:
            pass
        time.sleep(3)
    print("WARN GigaAM HTTP ещё не ответил — e2e_preflight проверит жёстко", flush=True)


def ensure_playwright_browsers() -> None:
    """Chromium + WebKit для мобильных/комнатных проверок."""
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    need = []
    if not any(base.glob("chromium-*")):
        need.append("chromium")
    if not any(base.glob("webkit-*")):
        need.append("webkit")
    if not need:
        print("Playwright browsers на месте", flush=True)
        return
    print(f"ставлю Playwright: {' '.join(need)}…", flush=True)
    subprocess.run([PY, "-m", "playwright", "install", *need], cwd=ROOT, timeout=600, check=False)


def ensure_android_sdk() -> None:
    """Проверка, что AVD pixel7 существует (boot делает qa_mobile_android --boot)."""
    avd = ROOT / "tools" / "android" / "avd" / "pixel7.ini"
    emu = ROOT / "tools" / "android" / "sdk" / "emulator" / "emulator.exe"
    if not avd.is_file() or not emu.is_file():
        print("FAIL: нет tools/android AVD pixel7 — кросс-платформа Android невозможна", flush=True)
        raise SystemExit(1)
    print("Android AVD pixel7 на месте (boot в qa_mobile_android --boot)", flush=True)


def mint_token_if_needed() -> None:
    if ENV.get("SMOKE_BEARER_TOKEN") or ENV.get("SMOKE_OTP_CODE"):
        return
    if ENV.get("E2E_ALLOW_MINT", "").strip() not in {"1", "true", "yes"}:
        # Для acceptance без OTP всё равно пробуем mint — иначе весь смоук бесполезен
        ENV["E2E_ALLOW_MINT"] = "1"
        print("E2E_ALLOW_MINT=1 (acceptance сам выпускает JWT)", flush=True)
    mint = subprocess.run(
        [PY, str(ROOT / "scripts" / "e2e" / "mint_smoke_token.py")],
        cwd=ROOT,
        env={**ENV, "E2E_ALLOW_MINT": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    token = (mint.stdout or "").strip().splitlines()[-1] if mint.returncode == 0 else ""
    if not token:
        print("FAIL mint_smoke_token:", mint.stderr or mint.stdout, flush=True)
        raise SystemExit(1)
    ENV["SMOKE_BEARER_TOKEN"] = token
    print("SMOKE_BEARER_TOKEN получен", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-evals", action="store_true")
    ap.add_argument("--skip-pytest", action="store_true")
    ap.add_argument("--skip-meetings", action="store_true")
    ap.add_argument("--skip-mobile", action="store_true")
    ap.add_argument("--materials-only", action="store_true")
    ap.add_argument("--skip-materials", action="store_true", help="не гонять e2e materials")
    ap.add_argument("--keep-materials", action="store_true", help="не чистить артефакты e2e")
    ap.add_argument("--evals", action="store_true", help="включить eval ТЗ/чата (по умолчанию вкл.)")
    args = ap.parse_args()
    do_evals = (args.evals or not args.skip_evals) and not args.materials_only
    if args.skip_evals:
        do_evals = False

    print("=== ensure runtime", flush=True)
    ensure_gigaam()
    ensure_playwright_browsers()
    if not args.skip_mobile and not args.materials_only:
        ensure_android_sdk()
    mint_token_if_needed()

    steps: list[dict] = []

    if not args.skip_materials:
        materials_cmd = [PY, str(ROOT / "scripts" / "e2e" / "run_materials.py")]
        if args.keep_materials:
            materials_cmd.append("--keep")
        steps.append(run("e2e materials (docs+audio+knowledge)", materials_cmd, timeout=14_400))
    elif not args.materials_only:
        # Пропуск materials только по флагу — в отчёте явно FAIL, если state не зелёный
        state_file = ROOT / "scripts" / "e2e" / "e2e_state.json"
        knowledge_ok = False
        tail = "--skip-materials без knowledge_ok"
        if state_file.is_file():
            try:
                st = json.loads(state_file.read_text(encoding="utf-8"))
                knowledge_ok = bool(st.get("knowledge_ok"))
                tail = f"--skip-materials knowledge_ok={knowledge_ok} audio={st.get('audio_id')}"
            except json.JSONDecodeError:
                pass
        steps.append(
            {
                "step": "e2e materials (docs+audio+knowledge)",
                "status": "OK" if knowledge_ok else "FAIL",
                "seconds": 0,
                "code": 0 if knowledge_ok else 1,
                "tail": tail,
            }
        )

    if not args.materials_only:
        if not args.skip_meetings:
            steps.append(run("smoke run_all", [PY, str(ROOT / "scripts" / "smoke" / "run_all.py")], timeout=3600))
            steps.append(
                run(
                    "qa_meeting_lifecycle",
                    [PY, str(ROOT / "scripts" / "qa_meeting_lifecycle.py"), "--linger", "30"],
                    timeout=1800,
                )
            )
            steps.append(run("qa_room", [PY, str(ROOT / "scripts" / "qa_room.py"), "--outage", "30"], timeout=1800))
            steps.append(run("qa_analysis", [PY, str(ROOT / "scripts" / "qa_analysis.py")], timeout=7200))
            steps.append(run("qa_media_path", [PY, str(ROOT / "scripts" / "qa_media_path.py")], timeout=900))
        if not args.skip_mobile:
            steps.append(run("qa_mobile_webkit", [PY, str(ROOT / "scripts" / "qa_mobile_webkit.py")], timeout=1800))
            # Всегда --boot: сами поднимаем pixel7, отсутствие устройства = FAIL
            steps.append(
                run(
                    "qa_mobile_android",
                    [PY, str(ROOT / "scripts" / "qa_mobile_android.py"), "--boot"],
                    timeout=2400,
                )
            )
        if not args.skip_pytest:
            steps.append(
                run(
                    "pytest (not gpu/slow)",
                    [PY, "-m", "pytest", "app", "-m", "not gpu and not slow", "-q", "-p", "no:cacheprovider"],
                    timeout=3600,
                )
            )
        steps.append(run("health_report", [PY, str(ROOT / "scripts" / "health_report.py")], timeout=300))
        steps.append(run("audit_integrity", [PY, str(ROOT / "scripts" / "audit_integrity.py")], timeout=600))
        if do_evals:
            def _first_existing(*paths: Path) -> Path | None:
                for path in paths:
                    if path.is_file():
                        return path
                return None

            facts = _first_existing(
                ROOT / "docs" / "eval" / "tz_facts_project18.json",
                ROOT / "data" / "eval" / "tz_facts_project18.json",
            )
            chat_q = _first_existing(
                ROOT / "docs" / "eval" / "chat_questions_project18.json",
                ROOT / "data" / "eval" / "chat_questions_project18.json",
                ROOT / "data" / "eval" / "chat_questions_messages_p18.json",
            )
            if facts is not None:
                steps.append(
                    run(
                        "eval_tz project 18",
                        [PY, str(ROOT / "scripts" / "eval_tz.py"), "--project", "18", "--runs", "1", "--facts", str(facts)],
                        timeout=3600,
                    )
                )
            else:
                steps.append(
                    {
                        "step": "eval_tz project 18",
                        "status": "FAIL",
                        "seconds": 0,
                        "code": 1,
                        "tail": "нет docs/eval/tz_facts_project18.json — набор обязателен для acceptance",
                    }
                )
            if chat_q is not None:
                steps.append(
                    run(
                        "eval_chat project 18",
                        [PY, str(ROOT / "scripts" / "eval_chat.py"), "--project", "18", "--questions", str(chat_q)],
                        timeout=3600,
                    )
                )
            else:
                steps.append(
                    {
                        "step": "eval_chat project 18",
                        "status": "FAIL",
                        "seconds": 0,
                        "code": 1,
                        "tail": "нет docs/eval/chat_questions_project18.json — набор обязателен для acceptance",
                    }
                )

    out_dir = ROOT / "data" / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / f"acceptance_{datetime.now():%Y%m%d_%H%M%S}.json"
    report.write_text(json.dumps(steps, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "\n".join(f"{s['status']:5} {s['step']}" for s in steps))
    print("отчёт:", report)
    failed = [s["step"] for s in steps if s["status"] == "FAIL"]
    if failed:
        print("FAIL:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
