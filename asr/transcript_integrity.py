"""Страховка от потерь текста: audit merge + UI и глобальный backfill."""

from __future__ import annotations

import json
import re
from typing import Any

from merge_guard import engine_reference_text, is_cursor_llm, merge_completeness, supplement_gemma_from_engines
from hallucination_guard import looks_like_loop, scrub_engine_text, scrub_merged_text

MIN_VIEW_VS_PLAIN = 0.90
MIN_GLOBAL_WORD_RECALL = 0.88
MAX_TAIL_GAP_SEC = 45.0
MAX_UI_HOLE_SEC = 55.0

_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+(?:-[0-9A-Za-zА-Яа-яЁё]+)?")


def _norm_words(text: str) -> set[str]:
    return {w.lower().replace("ё", "е") for w in _WORD_RE.findall(text or "") if len(w) >= 3}


def _norm_plain(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _word_recall(haystack: str, reference: str) -> float:
    ref = _norm_words(reference)
    if not ref:
        return 1.0
    got = _norm_words(haystack)
    return len(ref & got) / len(ref)


def _char_ratio(a: str, b: str) -> float:
    if not b:
        return 1.0 if not a else 0.0
    return len(a) / len(b)


def _stable_reference(giga_plain: str, tone_plain: str = "", whisper_plain: str = "") -> str:
    """Стабильный эталон ASR (gigaam/tone; раздутый Whisper не эталон)."""
    return engine_reference_text(whisper_plain, tone_plain, giga_plain)


def _merge_coverage_issues(body: str, *, giga: str, engine_ref: str) -> list[dict]:
    issues: list[dict] = []
    if giga:
        giga_recall = _word_recall(body, giga)
        giga_ratio = _char_ratio(body, giga)
        if giga_recall < MIN_GLOBAL_WORD_RECALL:
            issues.append(
                {
                    "kind": "plain_short_vs_giga",
                    "recall": round(giga_recall, 3),
                    "ratio": round(giga_ratio, 3),
                    "min_recall": MIN_GLOBAL_WORD_RECALL,
                }
            )
    ref = engine_ref or giga
    if ref:
        comp = merge_completeness(body, ref)
        if not comp["ok"]:
            issues.append(
                {
                    "kind": "global_merge_incomplete",
                    "word_recall": comp.get("word_recall"),
                    "char_ratio": comp.get("char_ratio"),
                    "reason": comp.get("reason"),
                    "reference_chars": len(ref),
                    "llm_provider": "cursor" if is_cursor_llm() else "lmstudio",
                }
            )
    return issues


def audit_window_seams(log: list[dict]) -> list[dict]:
    """Соседние окна без общих слов на стыке — риск неверной склейки."""
    rows = [e for e in log if (e.get("text") or "").strip() and not e.get("skipped")]
    issues: list[dict] = []
    for left, right in zip(rows, rows[1:]):
        lt = (left.get("text") or "").strip()
        rt = (right.get("text") or "").strip()
        if len(lt) < 80 or len(rt) < 80:
            continue
        tail = lt[-120:]
        head = rt[:120]
        if _word_recall(tail, head) >= 0.08:
            continue
        if _word_recall(head, tail) >= 0.08:
            continue
        issues.append(
            {
                "kind": "seam_discontinuity",
                "left_window": left.get("window"),
                "right_window": right.get("window"),
                "left_end": left.get("end"),
                "right_start": right.get("start"),
            }
        )
    return issues


def audit_merge_plain(
    plain: str,
    *,
    giga_plain: str,
    tone_plain: str = "",
    whisper_plain: str = "",
    log: list[dict],
    duration: float,
) -> dict[str, Any]:
    body = scrub_merged_text(plain) or (plain or "").strip()
    giga = scrub_engine_text(giga_plain)
    engine_ref = _stable_reference(giga_plain, tone_plain, whisper_plain)

    skipped = sum(1 for e in log if e.get("skipped") and not e.get("text"))
    still_fail = 0
    for e in log:
        ma = e.get("merge_audit") or {}
        aa = ma.get("audit_after") or {}
        if aa.get("ok"):
            continue
        recovery = ma.get("recovery") or "none"
        if recovery in ("engine_fallback", "supplement_recovered", "audit_fail_kept_llm"):
            continue
        still_fail += 1

    last_end = max((float(e.get("end") or 0) for e in log if e.get("text")), default=0.0)
    tail_gap = max(0.0, float(duration) - last_end) if duration else 0.0

    giga_ratio = _char_ratio(body, giga)
    engine_ratio = _char_ratio(body, engine_ref)
    global_recall = _word_recall(body, engine_ref or giga)

    seams = audit_window_seams(log)
    issues: list[dict] = _merge_coverage_issues(body, giga=giga, engine_ref=engine_ref)
    if skipped:
        issues.append({"kind": "skipped_windows", "count": skipped})
    if still_fail:
        issues.append({"kind": "merge_audit_still_fail", "count": still_fail})
    if duration and tail_gap > MAX_TAIL_GAP_SEC:
        issues.append(
            {
                "kind": "tail_not_covered",
                "tail_gap_sec": round(tail_gap, 1),
                "max": MAX_TAIL_GAP_SEC,
            }
        )
    if seams:
        issues.append({"kind": "seam_discontinuity", "count": len(seams), "samples": seams[:5]})

    status = "ok"
    if any(i["kind"] in ("plain_short_vs_giga", "global_merge_incomplete") for i in issues):
        status = "fail"
    elif issues:
        status = "degraded"

    return {
        "status": status,
        "plain_chars": len(body),
        "giga_chars": len(giga),
        "engine_chars": len(engine_ref),
        "plain_vs_giga": round(giga_ratio, 3),
        "plain_vs_engines": round(engine_ratio, 3),
        "global_word_recall": round(global_recall, 3),
        "tail_gap_sec": round(tail_gap, 1),
        "issues": issues,
    }


def repair_plain_from_engines(plain: str, reference: str) -> tuple[str, dict]:
    """Глобально добавить предложения ASR, которых нет в plain."""
    ref = scrub_engine_text(reference)
    body = scrub_merged_text(plain) or (plain or "").strip()
    if not ref or not body:
        return body, {"action": "noop", "added_chars": 0}

    before = merge_completeness(body, ref)
    if before.get("ok"):
        return body, {"action": "already_ok", "added_chars": 0}

    repaired, action = supplement_gemma_from_engines(body, ref)
    after = merge_completeness(repaired, ref)
    return repaired, {
        "action": action,
        "added_chars": max(0, len(repaired) - len(body)),
        "audit_before": before,
        "audit_after": after,
    }


def audit_and_repair_merge_plain(
    plain: str,
    *,
    giga_plain: str,
    tone_plain: str = "",
    whisper_plain: str = "",
    log: list[dict],
    duration: float,
) -> tuple[str, dict]:
    audit = audit_merge_plain(
        plain,
        giga_plain=giga_plain,
        tone_plain=tone_plain,
        whisper_plain=whisper_plain,
        log=log,
        duration=duration,
    )
    body = scrub_merged_text(plain) or (plain or "").strip()
    repair: dict[str, Any] = {"attempted": False}
    if audit["status"] == "fail":
        ref = _stable_reference(giga_plain, tone_plain, whisper_plain)
        if ref and not looks_like_loop(ref):
            repair["attempted"] = True
            body, repair_meta = repair_plain_from_engines(body, ref)
            repair.update(repair_meta)
            audit = audit_merge_plain(
                body,
                giga_plain=giga_plain,
                tone_plain=tone_plain,
                whisper_plain=whisper_plain,
                log=log,
                duration=duration,
            )
            audit["repair"] = repair
        else:
            audit["repair"] = {**repair, "action": "skip_loop_or_empty_ref"}
    else:
        audit["repair"] = repair
    return body, audit


def audit_view_turns(
    turns: list,
    *,
    plain: str,
    duration: float,
) -> dict[str, Any]:
    plain_body = scrub_merged_text(plain) or (plain or "").strip()
    turn_text = "\n".join((getattr(t, "text", "") or "").strip() for t in turns)
    turn_text = scrub_merged_text(turn_text) or turn_text.strip()

    recall = _word_recall(turn_text, plain_body)
    ratio = _char_ratio(turn_text, plain_body)
    last_end = max((float(getattr(t, "end", 0) or 0) for t in turns), default=0.0)
    tail_gap = max(0.0, float(duration) - last_end) if duration else 0.0

    holes: list[dict] = []
    ordered = sorted(turns, key=lambda t: (float(getattr(t, "start", 0)), float(getattr(t, "end", 0))))
    for left, right in zip(ordered, ordered[1:]):
        gap = float(getattr(right, "start", 0)) - float(getattr(left, "end", 0))
        if gap > MAX_UI_HOLE_SEC:
            holes.append(
                {
                    "start": round(float(getattr(left, "end", 0)), 1),
                    "end": round(float(getattr(right, "start", 0)), 1),
                    "gap_sec": round(gap, 1),
                }
            )

    issues: list[dict] = []
    if plain_body and recall < MIN_VIEW_VS_PLAIN:
        issues.append(
            {
                "kind": "view_text_loss",
                "recall": round(recall, 3),
                "ratio": round(ratio, 3),
                "min_recall": MIN_VIEW_VS_PLAIN,
            }
        )
    if duration and tail_gap > MAX_TAIL_GAP_SEC:
        issues.append(
            {
                "kind": "view_tail_gap",
                "tail_gap_sec": round(tail_gap, 1),
                "max": MAX_TAIL_GAP_SEC,
            }
        )
    if holes:
        issues.append({"kind": "view_time_holes", "count": len(holes), "samples": holes[:8]})

    status = "ok"
    if any(i["kind"] == "view_text_loss" for i in issues):
        status = "fail"
    elif issues:
        status = "degraded"

    return {
        "status": status,
        "turns": len(turns),
        "plain_chars": len(plain_body),
        "view_chars": len(turn_text),
        "plain_vs_view_recall": round(recall, 3),
        "plain_vs_view_ratio": round(ratio, 3),
        "last_turn_end": round(last_end, 1),
        "tail_gap_sec": round(tail_gap, 1),
        "issues": issues,
    }


def write_transcript_integrity(
    out_dir,
    *,
    merge_audit: dict | None = None,
    view_audit: dict | None = None,
) -> dict:
    payload = {
        "merge": merge_audit or {},
        "view": view_audit or {},
    }
    statuses = [x.get("status") for x in (merge_audit, view_audit) if isinstance(x, dict) and x.get("status")]
    if "fail" in statuses:
        payload["status"] = "fail"
    elif "degraded" in statuses:
        payload["status"] = "degraded"
    else:
        payload["status"] = "ok"
    path = out_dir / "transcript_integrity.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
