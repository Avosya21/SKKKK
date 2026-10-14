"""Ссылка на место источника (docs/plans/SOURCE-ANCHORS-PLAN.md, этап 4): момент созвона, сообщение переписки, абзац документа.

Страница отчёта (`/artifacts/<id>/report`) понимает параметры: ``t`` — секунда созвона (реплика подсвечивается, плеер
перематывается без воспроизведения, Я4), ``m`` — номер сообщения переписки, ``q`` — начало цитаты документа. Переход делает
``static/report-jump.js``, который ``pages._serve_report`` подмешивает при отдаче — сохранённые отчёты не пересобираются.
"""

from __future__ import annotations

from urllib.parse import urlencode

QUOTE_CHARS = 80


def source_url(artifact_id: str | None, *, start_sec: float | None = None, message_id: int | None = None,
               quote: str | None = None) -> str | None:
    if not artifact_id:
        return None
    base = f"/artifacts/{artifact_id}/report"
    if start_sec is not None:
        return f"{base}?{urlencode({'t': f'{float(start_sec):.1f}'})}"
    text = " ".join((quote or "").split())[:QUOTE_CHARS]
    if message_id is not None:  # якорь сообщения; цитата — запасной путь, если отчёт ещё без якорей
        return f"{base}?{urlencode({'m': int(message_id), **({'q': text} if text else {})})}"
    return f"{base}?{urlencode({'q': text})}" if text else base


def unit_number(ref: str | None, prefix: str) -> int | None:
    """Номер из адреса единицы: ``unit_number("M105", "M")`` → 105; другой вид адреса или пусто — None."""
    ref = (ref or "").strip()
    if not ref.startswith(prefix) or not ref[len(prefix):].lstrip("-").isdigit():
        return None
    return int(ref[len(prefix):])
