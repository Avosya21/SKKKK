"""Enroll speaker_N; в strict — speakers-confirm → done; в soft — enroll + проверка статуса API.

Soft-режим продукта (R3) не останавливает конвейер на speakers_pending — confirm может быть недоступен.
Тогда обязаны хотя бы enroll'ить метки (иначе голоса навечно speaker_N).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import (  # noqa: E402
    artifact_dir,
    ensure_login,
    fail,
    get_artifact,
    ok,
    read_state,
    state_path,
    wait_status,
    write_state,
)

CONFIRM_TIMEOUT = 3600
NAMES = [
    "E2E Спикер Альфа",
    "E2E Спикер Бета",
    "E2E Спикер Гамма",
    "E2E Спикер Дельта",
    "E2E Спикер Эпсилон",
    "E2E Спикер Дзета",
    "E2E Спикер Эта",
    "E2E Спикер Тета",
]


def main() -> int:
    st = read_state(state_path())
    aid = st.get("audio_id")
    if not aid:
        fail("нет audio_id — сначала e2e_audio.py")
    client, _ = ensure_login()
    mode = st.get("voice_gate_mode") or "soft"
    row = get_artifact(client, aid) or {}
    status = str(row.get("status") or "")

    r = client.get(f"/api/artifacts/{aid}/speakers-status")
    if r.status_code >= 400:
        fail(f"speakers-status: HTTP {r.status_code} {r.text[:300]}")
    info = r.json()
    pending = list(info.get("pending") or [])
    if not pending:
        labels = list(info.get("labels") or st.get("audio_labels") or [])
        confirmed = set(info.get("confirmed") or [])
        pending = [x for x in labels if x not in confirmed]
    if not pending and not info.get("ready"):
        fail(f"нет меток спикеров: {info}")

    if pending:
        ok(f"метки к enroll: {pending}")
        for i, label in enumerate(pending):
            name = NAMES[i % len(NAMES)]
            er = client.post(
                f"/api/artifacts/{aid}/speaker-enroll",
                json={"diar_label": label, "display_name": name},
                timeout=180,
            )
            if er.status_code >= 400:
                fail(f"enroll {label}: HTTP {er.status_code} {er.text[:300]}")
            ok(f"enroll {label} → {name}")
            time.sleep(0.5)
    else:
        ok("все метки уже confirmed")

    r = client.get(f"/api/artifacts/{aid}/speakers-status")
    r.raise_for_status()
    info = r.json()
    if not info.get("ready"):
        fail(f"после enroll не ready: {info}")

    if status == "speakers_pending" or mode == "strict":
        cr = client.post(f"/api/artifacts/{aid}/speakers-confirm", timeout=60)
        if cr.status_code >= 400:
            fail(f"speakers-confirm: HTTP {cr.status_code} {cr.text[:400]}")
        ok(f"speakers-confirm → {cr.json()}")
        wait_status(client, aid, {"done"}, timeout_sec=CONFIRM_TIMEOUT, label="knowledge")
    else:
        # soft: confirm на done даст 400 — это ожидаемо; имена уже через enroll
        ok(f"soft/gate: status={status}, confirm не требуется (R3)")
        if status not in {"done", "transcript_ready", "speakers_confirmed"}:
            fail(f"после ASR неожиданный status={status}")

    base = artifact_dir(aid)
    proof = base / "proof" / "transcript.txt"
    finals = list((base / "parsed").glob("*_final.txt")) if (base / "parsed").is_dir() else []
    # soft может ещё не иметь proof до knowledge — проверим lab/publish
    if not proof.is_file() and not finals:
        lab_txt = list((base / "lab").rglob("*.txt")) if (base / "lab").is_dir() else []
        if not lab_txt:
            fail(f"нет proof/final/lab текста после speakers в {base}")
        ok("текст ASR на месте (lab)")
    else:
        body = proof.read_text(encoding="utf-8", errors="replace") if proof.is_file() else finals[0].read_text(encoding="utf-8", errors="replace")
        if len(body.strip()) < 40:
            fail(f"стенограмма слишком короткая ({len(body)} chars)")
        ok(f"стенограмма ({len(body)} chars)")

    st["audio_confirmed"] = True
    write_state(state_path(), st)
    print(f"speakers OK → {aid}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError:
        raise SystemExit(1)
