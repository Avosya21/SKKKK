"""После материалов: search по якорю → rebuild-tz → chat с источниками.

Мягкие fallback («нашлось хоть что-то») запрещены — иначе тест не ловит дыры индекса/RAG.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import (  # noqa: E402
    ANCHOR,
    PID,
    ensure_login,
    fail,
    ok,
    read_state,
    state_path,
    wait_project_task_idle,
    write_state,
)

SEARCH_TIMEOUT = 180
TZ_TIMEOUT = 1200
CHAT_TIMEOUT = 600


def _hit_blob(hits: list) -> str:
    return str(hits).lower()


def main() -> int:
    st = read_state(state_path())
    anchor = st.get("anchor") or ANCHOR
    if not st.get("document_ids") and not st.get("audio_id"):
        fail("e2e_state пуст — сначала documents/audio")
    client, _ = ensure_login()

    # --- search: только точный якорь ---
    deadline = time.time() + SEARCH_TIMEOUT
    hits: list = []
    while time.time() < deadline:
        r = client.get(f"/projects/{PID}/search", params={"q": anchor, "limit": 10})
        if r.status_code >= 400:
            fail(f"search: HTTP {r.status_code} {r.text[:300]}")
        data = r.json()
        hits = data.get("results") or data.get("items") or []
        if hits and anchor.lower() in _hit_blob(hits):
            break
        time.sleep(3)
    if not hits:
        fail(f"search по якорю {anchor!r} пуст — индекс/эмбеддинги/нарезка сломаны")
    if anchor.lower() not in _hit_blob(hits):
        fail(f"search вернул хиты без якоря {anchor!r}: {hits[:2]}")
    ok(f"search якорь hits={len(hits)}")

    # --- rebuild TZ ---
    r = client.post(f"/projects/{PID}/rebuild-tz")
    if r.status_code == 409:
        ok("rebuild-tz уже в очереди")
    elif r.status_code >= 400:
        fail(f"rebuild-tz: HTTP {r.status_code} {r.text[:300]}")
    else:
        ok(f"rebuild-tz queued: {r.json()}")
    wait_project_task_idle(client, timeout_sec=TZ_TIMEOUT)

    r = client.get(f"/projects/{PID}/tz/content")
    if r.status_code >= 400:
        fail(f"tz/content: HTTP {r.status_code} {r.text[:300]}")
    body = r.json() if "application/json" in (r.headers.get("content-type") or "") else {"text": r.text}
    text = str(body.get("content") or body.get("text") or body.get("markdown") or "")
    if len(text.strip()) < 80:
        fail(f"ТЗ пустое/короткое после rebuild ({len(text)} chars)")
    # якорь или явные слова из фикстур должны попасть в выжимку
    low = text.lower()
    if anchor.lower() not in low and "приёмк" not in low and "e2e" not in low:
        fail(f"ТЗ не содержит якорь/тему e2e — выжимка оторвана от материалов ({text[:240]!r})")
    ok(f"tz content ({len(text)} chars, тема на месте)")

    # --- chat ---
    question = f"Что сказано про {anchor}? Ответь по материалам песочницы и укажи источники."
    r = client.post("/chat", json={"project_id": PID, "question": question}, timeout=CHAT_TIMEOUT)
    answer: dict
    if r.status_code == 409:
        deadline = time.time() + CHAT_TIMEOUT
        while time.time() < deadline:
            stt = client.get(f"/projects/{PID}/chat/status").json()
            if not stt.get("pending"):
                break
            time.sleep(2)
        hist = client.get(f"/projects/{PID}/chat/history").json()
        msgs = hist.get("messages") or []
        answer = (msgs[-1] if msgs else {}) or {}
    elif r.status_code >= 400:
        fail(f"chat: HTTP {r.status_code} {r.text[:400]}")
    else:
        answer = r.json()

    ans_text = str(answer.get("answer") or answer.get("text") or answer.get("content") or "")
    if len(ans_text.strip()) < 20:
        fail(f"пустой/короткий ответ чата: {answer}")
    if anchor.lower() not in ans_text.lower() and "не знаю" in ans_text.lower():
        fail(f"чат отказался при наличии якоря в индексе: {ans_text[:300]!r}")
    sources = answer.get("sources") or answer.get("refs") or []
    # источники могут быть вложены в meta
    if not sources and isinstance(answer.get("meta"), dict):
        sources = answer["meta"].get("sources") or []
    if not sources:
        fail(f"чат без источников — RAG не привязал ответ к материалам: {list(answer.keys())}")
    ok(f"chat answer ({len(ans_text)} chars, sources={len(sources)})")

    st["knowledge_ok"] = True
    write_state(state_path(), st)
    print("knowledge OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError:
        raise SystemExit(1)
