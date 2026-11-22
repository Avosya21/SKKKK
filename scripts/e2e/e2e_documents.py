"""Загрузка всех типов документов → done + текст с якорем на диске/в отчёте."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from build_fixtures import main as build_fixtures  # noqa: E402
from common import (  # noqa: E402
    ANCHOR,
    DOC_SOURCE,
    FIXTURES,
    artifact_dir,
    delete_artifact,
    ensure_login,
    fail,
    ok,
    read_state,
    state_path,
    upload_file,
    wait_status,
    write_state,
)

DOC_SPECS = [
    ("e2e_note.txt", "text/plain"),
    ("e2e_page.html", "text/html"),
    ("e2e_export.json", "application/json"),
    ("e2e_protocol.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("e2e_protocol.odt", "application/vnd.oasis.opendocument.text"),
    ("e2e_estimate.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("e2e_table.csv", "text/csv"),
    ("e2e_slides.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ("e2e_doc.pdf", "application/pdf"),
    ("e2e_scan.png", "image/png"),
]

DOC_TIMEOUT = 900


def _artifact_has_anchor(aid: str) -> bool:
    base = artifact_dir(aid)
    chunks: list[str] = []
    for folder in ("parsed", "reports", "text"):
        d = base / folder
        if not d.is_dir():
            continue
        for path in d.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".txt", ".md", ".html", ".json", ".clean"}:
                try:
                    chunks.append(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
    # сырой оригинал тоже
    for path in base.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".txt", ".html", ".json"}:
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    blob = "\n".join(chunks)
    return ANCHOR in blob


def main() -> int:
    build_fixtures()
    client, _ = ensure_login()
    ids: list[str] = []
    for name, mime in DOC_SPECS:
        path = FIXTURES / name
        if not path.is_file():
            fail(f"нет фикстуры {path}")
        if path.suffix.lower() in {".txt", ".html", ".json"}:
            raw = path.read_text(encoding="utf-8", errors="replace")
            if ANCHOR not in raw:
                fail(f"фикстура {name} без якоря {ANCHOR}")
        row = upload_file(client, path, source_type=DOC_SOURCE, content_type=mime)
        aid = row["artifact_id"]
        ids.append(aid)
        wait_status(client, aid, {"done"}, timeout_sec=DOC_TIMEOUT, label="document")

        rep = client.get(f"/artifacts/{aid}/report", follow_redirects=True)
        if rep.status_code != 200:
            fail(f"report {aid}: HTTP {rep.status_code}")
        if "text/html" not in (rep.headers.get("content-type") or "") and len(rep.text) < 20:
            fail(f"report {aid}: пустой ответ")
        # якорь обязан появиться в извлечённом тексте.
        # PNG без Pillow — крошечный 1×1 без текста: достаточно done + непустой report.
        is_tiny_png = name.endswith(".png") and (FIXTURES / name).stat().st_size < 500
        if not is_tiny_png:
            if not _artifact_has_anchor(aid) and ANCHOR not in rep.text:
                fail(f"{name}: после done нет якоря {ANCHOR} в parsed/reports — извлечение сломано")
            ok(f"report+якорь {name}")
        else:
            ok(f"report+done {name} (png без OCR-текста)")

    st = read_state(state_path())
    st.update({"document_ids": ids, "anchor": ANCHOR})
    write_state(state_path(), st)
    print(f"documents OK ({len(ids)}), anchor={ANCHOR}")
    return 0


def cleanup(client=None) -> None:
    own = client is None
    if own:
        client, _ = ensure_login()
    st = read_state(state_path())
    for aid in st.get("document_ids") or []:
        try:
            delete_artifact(client, aid)
        except AssertionError as exc:
            print(f"WARN cleanup doc {aid}: {exc}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError:
        raise SystemExit(1)
