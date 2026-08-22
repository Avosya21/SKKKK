"""Проверка полноты склейки LLM и восстановление пропусков из ASR."""

from __future__ import annotations

import difflib
import os
import re

from clock_guard import fallback_budget_sec
from hallucination_guard import (
    MAX_CHARS_PER_SEC,
    _SENT_SPLIT_RE,
    looks_like_loop,
    scrub_engine_text,
    scrub_merged_text,
)

# Окно LLM должно покрывать лексику эталонного черновика ASR.
MIN_WORD_RECALL = 0.82
MIN_CHAR_RATIO = 0.72
# Cursor/Composer «причёсывает» текст: короче раздутого Whisper, но не короче gigaam/tone.
MIN_CHAR_RATIO_CURSOR = 0.45
HIGH_WORD_RECALL = 0.88
# Whisper с петлями часто в 1.3–2× длиннее gigaam на том же окне.
WHISPER_OUTLIER_RATIO = 1.30

_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+(?:-[0-9A-Za-zА-Яа-яЁё]+)?")


def is_cursor_llm() -> bool:
    raw = (os.environ.get("LLM_PROVIDER") or "lmstudio").strip().lower()
    return raw == "cursor"


def _norm_words(text: str) -> set[str]:
    return {w.lower().replace("ё", "е") for w in _WORD_RE.findall(text or "") if len(w) >= 3}


def _scrubbed_engines(whisper: str, tone: str, gigaam: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, raw in (("gigaam", gigaam), ("tone", tone), ("whisper", whisper)):
        body = scrub_engine_text(raw)
        if body and not looks_like_loop(body):
            out[key] = body
    return out


def engine_reference_text(whisper: str, tone: str, gigaam: str) -> str:
    """Эталон для audit: gigaam/tone; раздутый Whisper не эталон, если есть другие движки."""
    engines = _scrubbed_engines(whisper, tone, gigaam)
    if not engines:
        return ""

    alt = [engines[k] for k in ("gigaam", "tone") if k in engines]
    whis = engines.get("whisper", "")
    if whis and alt:
        baseline = max(len(x) for x in alt)
        if baseline > 0 and len(whis) > baseline * WHISPER_OUTLIER_RATIO:
            return max(alt, key=len)

    return max(engines.values(), key=len)


def _min_char_ratio() -> float:
    return MIN_CHAR_RATIO if not is_cursor_llm() else MIN_CHAR_RATIO_CURSOR


def _audit_ok(*, recall: float, ratio: float, body: str) -> bool:
    min_ratio = _min_char_ratio()
    if recall >= MIN_WORD_RECALL and ratio >= min_ratio:
        return True
    # Cursor: высокий recall + без петель — «причёсанный» текст, не раздуваем до Whisper-loop.
    if (
        is_cursor_llm()
        and recall >= HIGH_WORD_RECALL
        and body
        and not looks_like_loop(body)
        and ratio >= min_ratio
    ):
        return True
    return False


def merge_budget_sec(
    speech_sec: float,
    window_span: float,
    whisper: str,
    tone: str,
    gigaam: str,
    *,
    alive_n: int,
) -> float:
    """Бюджет символов окна: diar часто занижает длительность речи в длинном окне."""
    engine_chars = max((len(x) for x in (whisper, tone, gigaam) if x), default=0)
    budget = fallback_budget_sec(speech_sec, window_span)
    if engine_chars:
        budget = max(budget, engine_chars / MAX_CHARS_PER_SEC)
    if alive_n >= 2 and engine_chars:
        budget = max(budget, engine_chars / MAX_CHARS_PER_SEC)
    return budget


def merge_completeness(gemma_text: str, reference: str) -> dict:
    body = (gemma_text or "").strip()
    ref = (reference or "").strip()
    if not ref:
        return {
            "ok": bool(body),
            "word_recall": 1.0 if body else 0.0,
            "char_ratio": 1.0 if body else 0.0,
            "reason": "no_reference",
            "missing_words_sample": [],
        }
    if not body:
        return {
            "ok": False,
            "word_recall": 0.0,
            "char_ratio": 0.0,
            "reason": "empty_gemma",
            "missing_words_sample": sorted(_norm_words(ref))[:12],
        }
    rw = _norm_words(ref)
    gw = _norm_words(body)
    recall = len(rw & gw) / max(len(rw), 1)
    ratio = len(body) / max(len(ref), 1)
    min_ratio = _min_char_ratio()
    ok = _audit_ok(recall=recall, ratio=ratio, body=body)
    missing = sorted(rw - gw, key=len, reverse=True)[:12]
    reason = "ok"
    if not ok:
        parts = []
        if recall < MIN_WORD_RECALL:
            parts.append(f"word_recall={recall:.2f}<{MIN_WORD_RECALL}")
        if ratio < min_ratio:
            parts.append(f"char_ratio={ratio:.2f}<{min_ratio}")
        reason = "; ".join(parts)
    return {
        "ok": ok,
        "word_recall": round(recall, 3),
        "char_ratio": round(ratio, 3),
        "reason": reason,
        "missing_words_sample": missing,
    }


def _sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENT_SPLIT_RE.split(text or "") if s.strip()]
    return parts if parts else ([text.strip()] if (text or "").strip() else [])


def _sentence_in_gemma(sentence: str, gemma_norm: str) -> bool:
    sn = re.sub(r"\s+", " ", (sentence or "").strip().lower().replace("ё", "е"))
    if len(sn) < 10:
        return True
    if sn in gemma_norm:
        return True
    probe = sn[: min(48, len(sn))]
    return gemma_norm.count(probe) > 0 or difflib.SequenceMatcher(None, probe, gemma_norm).ratio() >= 0.72


def supplement_gemma_from_engines(gemma_text: str, reference: str) -> tuple[str, str]:
    """Добавляет предложения из ASR, которых нет в ответе Gemma."""
    body = scrub_merged_text(gemma_text)
    ref = scrub_engine_text(reference)
    if not body or not ref:
        return body, "unchanged"
    audit = merge_completeness(body, ref)
    if audit["ok"]:
        return body, "ok"

    gemma_norm = re.sub(r"\s+", " ", body.lower().replace("ё", "е"))
    added: list[str] = []
    for sent in _sentences(ref):
        if len(sent) < 12:
            continue
        if _sentence_in_gemma(sent, gemma_norm):
            continue
        added.append(sent)
        gemma_norm += " " + sent.lower()

    if not added:
        return body, "no_sentences_matched"

    merged = body.rstrip()
    if merged and not merged.endswith((".", "!", "?", "…")):
        merged += "."
    merged += " " + " ".join(added)
    merged = scrub_merged_text(merged) or merged.strip()
    after = merge_completeness(merged, ref)
    action = "supplemented" if after["ok"] or len(merged) > len(body) * 1.08 else "supplement_weak"
    return merged, action


def finalize_window_text(
    gemma_text: str,
    whisper: str,
    tone: str,
    gigaam: str,
    *,
    speech_sec: float,
    window_span: float,
    alive_n: int,
) -> tuple[str, dict]:
    """Cap по реальному бюджету окна + audit + supplement при неполной Gemma."""
    from hallucination_guard import cap_text_to_speech_budget

    ref = engine_reference_text(whisper, tone, gigaam)
    budget_sec = merge_budget_sec(
        speech_sec, window_span, whisper, tone, gigaam, alive_n=alive_n
    )
    text = scrub_merged_text(gemma_text) or ""
    before_cap = text
    capped = cap_text_to_speech_budget(before_cap, budget_sec)
    text = capped if capped else before_cap
    if not text or looks_like_loop(text):
        return "", {
            "audit_before": merge_completeness("", ref),
            "audit_after": merge_completeness("", ref),
            "action": "reject_empty_or_loop",
            "budget_sec": round(budget_sec, 2),
            "reference_chars": len(ref),
        }

    audit_before = merge_completeness(text, ref)
    action = "ok"
    if ref and not audit_before["ok"]:
        text, action = supplement_gemma_from_engines(text, ref)
        before_cap = text
        capped = cap_text_to_speech_budget(before_cap, budget_sec)
        text = capped if capped else before_cap

    audit_after = merge_completeness(text, ref)
    return text, {
        "audit_before": audit_before,
        "audit_after": audit_after,
        "action": action,
        "budget_sec": round(budget_sec, 2),
        "reference_chars": len(ref),
    }


def recover_failed_audit(
    gemma_text: str,
    whisper: str,
    tone: str,
    gigaam: str,
    *,
    whisper_raw: str = "",
    tone_raw: str = "",
    gigaam_raw: str = "",
    speech_sec: float,
    window_span: float,
    alive_n: int,
    merge_meta: dict,
) -> tuple[str, dict]:
    """Автовосстановление окна, если audit после Gemma/retry/supplement не прошёл."""
    from hallucination_guard import cap_text_to_speech_budget, pick_fallback_engine_text

    meta = dict(merge_meta or {})
    audit = meta.get("audit_after") or {}
    if audit.get("ok"):
        meta["recovery"] = "none"
        return gemma_text, meta

    ref = engine_reference_text(
        whisper_raw or whisper,
        tone_raw or tone,
        gigaam_raw or gigaam,
    )
    budget_sec = float(meta.get("budget_sec") or 0.0)
    if budget_sec <= 0:
        budget_sec = merge_budget_sec(
            speech_sec, window_span, whisper, tone, gigaam, alive_n=alive_n
        )

    body = (gemma_text or "").strip()
    if ref and body:
        supplemented, sup_action = supplement_gemma_from_engines(body, ref)
        if supplemented and len(supplemented) > len(body):
            capped = cap_text_to_speech_budget(supplemented, budget_sec)
            candidate = capped if capped else supplemented
            sup_audit = merge_completeness(candidate, ref)
            if sup_audit["ok"] or sup_audit["char_ratio"] > audit.get("char_ratio", 0):
                body = candidate
                audit = sup_audit
                meta["action"] = sup_action
                meta["audit_after"] = sup_audit
                if sup_audit["ok"]:
                    meta["recovery"] = "supplement_recovered"
                    return body, meta

    cur_audit = merge_completeness(body, ref) if body else {"ok": False, "word_recall": 0.0}
    if (
        body
        and not looks_like_loop(body)
        and cur_audit.get("word_recall", 0) >= MIN_WORD_RECALL
        and (
            cur_audit.get("ok")
            or (is_cursor_llm() and cur_audit.get("word_recall", 0) >= HIGH_WORD_RECALL)
        )
    ):
        meta["recovery"] = "audit_fail_kept_llm"
        meta["audit_before"] = cur_audit
        meta["audit_after"] = cur_audit
        return body, meta

    fallback = pick_fallback_engine_text(
        whisper_raw or whisper,
        tone_raw or tone,
        gigaam_raw or gigaam,
        speech_sec=budget_sec,
        window_span=window_span,
    )
    if not fallback or looks_like_loop(fallback):
        meta["recovery"] = "audit_fail_no_engine" if not fallback else "audit_fail_loop_engine"
        return body, meta

    fb_audit = merge_completeness(fallback, ref)
    use_fallback = fb_audit["ok"] or (
        not cur_audit.get("ok")
        and not looks_like_loop(fallback)
        and (
            fb_audit.get("word_recall", 0) > cur_audit.get("word_recall", 0) + 0.03
            or fb_audit.get("char_ratio", 0) > cur_audit.get("char_ratio", 0) + 0.03
            or len(fallback) > len(body) * 1.1
        )
    )
    if use_fallback:
        meta.update(
            {
                "audit_before": cur_audit,
                "audit_after": fb_audit,
                "action": "engine_fallback_after_audit",
                "recovery": "engine_fallback",
                "gemma_chars_kept": len(body),
                "fallback_chars": len(fallback),
            }
        )
        return fallback, meta

    meta["recovery"] = "audit_fail_kept_gemma"
    return body, meta
