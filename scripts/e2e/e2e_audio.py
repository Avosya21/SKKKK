"""Короткое русское аудио → dual ASR + диаризация.

- ``VOICE_GATE_MODE=strict``: обязаны остановиться на ``speakers_pending``.
- ``soft`` (дефолт продукта): идут в ``transcript_ready``/``done`` без ожидания человека —
  это не дыра, но match + текст ASR обязаны быть.
Прыжок в error / «речь не обнаружена» / нет match — FAIL.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_fixtures import main as build_fixtures  # noqa: E402
from common import (  # noqa: E402
    AUDIO_SOURCE,
    FIXTURES,
    artifact_dir,
    delete_artifact,
    ensure_login,
    fail,
    get_artifact,
    ok,
    read_state,
    state_path,
    upload_file,
    wait_status,
    write_state,
)

AUDIO_TIMEOUT = 5400


def _gate_mode() -> str:
    from app.app.config import get_settings
    from app.app.services.speaker_auto import gate_mode

    return gate_mode(get_settings())


def main() -> int:
    build_fixtures()
    wav = FIXTURES / "e2e_speech.wav"
    if not wav.is_file():
        fail(f"нет {wav}")
    client, _ = ensure_login()
    row = upload_file(client, wav, source_type=AUDIO_SOURCE, content_type="audio/wav")
    aid = row["artifact_id"]
    if str(row.get("status") or "") in {"done", "speakers_confirmed", "transcript_ready"}:
        fail(
            f"upload вернул уже готовый артефакт {aid} status={row.get('status')} — "
            "дедуп sha256; нужен уникальный WAV (build_fixtures)"
        )

    mode = _gate_mode()
    ok(f"voice_gate_mode={mode}")
    if mode == "strict":
        want = {"speakers_pending"}
    else:
        # soft/off: конвейер не ждёт человека (R3), но ASR обязан завершиться
        want = {"speakers_pending", "transcript_ready", "done"}

    wait_status(client, aid, want, timeout_sec=AUDIO_TIMEOUT, label="audio")
    row = get_artifact(client, aid) or {}
    status = str(row.get("status") or "")
    if mode == "strict" and status != "speakers_pending":
        fail(f"strict: ждали speakers_pending, получили {status!r}")

    base = artifact_dir(aid)
    match = base / "reports" / "sortformer_speaker_match.json"
    if not match.is_file():
        fail(f"нет {match} — диаризация/match не записали метки")
    try:
        payload = json.loads(match.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"sortformer_speaker_match.json битый: {exc}")
    labels = list((payload.get("matches") or {}).keys()) or list((payload.get("embeddings") or {}).keys())
    if not labels:
        fail(f"match без меток speaker_N: keys={list(payload.keys())}")
    ok(f"match labels={labels}")

    text_blobs: list[str] = []
    candidates: list[Path] = [base / "proof" / "transcript.txt"]
    if (base / "lab").is_dir():
        candidates.extend(list((base / "lab").rglob("*.txt"))[:20])
    if (base / "asr").is_dir():
        candidates.extend(list((base / "asr").rglob("*.txt"))[:20])
    for path in candidates:
        if path.is_file() and path.stat().st_size > 0:
            text_blobs.append(path.read_text(encoding="utf-8", errors="replace"))
    joined = "\n".join(text_blobs).lower()
    if not joined.strip():
        fail(f"нет текстовых артефактов ASR в {base}")
    if "речь не обнаружена" in joined:
        fail("ASR написал «речь не обнаружена» на файле с речью")
    letters = sum(ch.isalpha() for ch in joined)
    if letters < 40:
        fail(f"ASR текст слишком короткий ({letters} букв)")
    ok(f"ASR текст есть ({letters} букв)")

    st = read_state(state_path())
    st.update({"audio_id": aid, "audio_labels": labels, "voice_gate_mode": mode, "audio_status": status})
    write_state(state_path(), st)
    print(f"audio OK → {aid} ({status}, gate={mode})")
    return 0


def cleanup(client=None) -> None:
    own = client is None
    if own:
        client, _ = ensure_login()
    aid = read_state(state_path()).get("audio_id")
    if aid:
        try:
            delete_artifact(client, aid)
        except AssertionError as exc:
            print(f"WARN cleanup audio {aid}: {exc}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError:
        raise SystemExit(1)
