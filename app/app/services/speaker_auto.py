"""Спикеры без лишних подтверждений (этап 3 docs/plans/AUTO-REVIEW-PLAN.md).

1. Автопринятие узнанного голоса: лучший профиль-кандидат WeSpeaker с cosine ≥ ``voice_auto_accept_min`` и отрывом от
   второго ≥ ``voice_auto_accept_margin`` → голос подтверждается с пометкой ``auto``. На истории (76 голосов) правило
   0.85/0.10 не дало ни одной ошибки; отрыв отсекает одного человека в двух профилях. Профиль при этом не дообучается.
   Имя сразу идёт в стенограмму, но с 15.10 голос помечен ``needs_human``: кнопка «Подтвердить спикеров» и зелёная
   «Голоса» ждут, пока человек проверит карточку (имя, компания, сторона — ``speaker_cards``).
2. Шлагбаум: ``strict`` ждёт, пока все голоса названы; ``soft`` строит знания сразу, неназванные голоса остаются ``speaker_N``.
3. Подсказка имени неизвестного голоса моделью — по обращениям и самопредставлениям в тексте. Только подсказка в карточке:
   имя даёт человек («Запомнить»).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.app.config import Settings
from app.app.logging_setup import log_event

logger = logging.getLogger("primaryanalitics.speakers")

ROOT = Path(__file__).resolve().parents[3]
PROMPT_PATH = ROOT / "docs" / "prompts" / "suggest_speaker_names.md"
PROMPT_VERSION = "suggest_speaker_names-v1"
GATE_STRICT = "strict"
GATE_SOFT = "soft"
GATE_OFF = "off"
HINT_MIN_CONFIDENCE = 0.6
HINT_TURNS_PER_LABEL = 12
HINT_TURN_CHARS = 400
PLACEHOLDER_LABEL = re.compile(r"^(speaker_\d+|SPEAKER_\d+)$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def gate_mode(settings: Settings) -> str:
    if not settings.voice_gate_before_asr:
        return GATE_OFF
    mode = str(settings.voice_gate_mode or "").strip().lower()
    return mode if mode in (GATE_STRICT, GATE_SOFT, GATE_OFF) else GATE_STRICT


def gate_proceeds(settings: Settings, unnamed: list[str]) -> bool:
    """Идти ли к знаниям без ожидания человека: soft / off — да; strict — только если все голоса названы."""
    mode = gate_mode(settings)
    return mode != GATE_STRICT or not unnamed


def _person(name: str, aliases: dict[str, str]) -> str:
    key = str(name or "").strip()
    return str(aliases.get(key) or key).strip().casefold()


def auto_accept_decision(
    candidates: list[dict[str, Any]], settings: Settings, aliases: dict[str, str] | None = None
) -> dict[str, Any] | None:
    """Решение по одному голосу: принять лучший профиль, если сходство высокое и отрыв от второго ЧЕЛОВЕКА заметный.

    Профили одного человека (``voice_aliases`` lab: «Заказчик 2 Клиент Б» → «Сергей Орлов») имеют почти одинаковое
    сходство; без учёта этого отрыва нет никогда и узнанный голос не принимается (прод 2026-09-24, проект 37).
    """
    if not candidates:
        return None
    aliases = aliases or {}
    top = candidates[0] or {}
    s1 = top.get("cosine")
    if s1 is None or not top.get("profile_id") or not str(top.get("display_name") or "").strip():
        return None
    who = _person(top["display_name"], aliases)
    other = next((c for c in candidates[1:] if c and _person(c.get("display_name") or "", aliases) != who), None)
    s2 = (other or {}).get("cosine") or 0.0
    margin = float(s1) - float(s2)
    if float(s1) < settings.voice_auto_accept_min or margin < settings.voice_auto_accept_margin:
        return None
    return {
        "profile_id": top["profile_id"],
        "profile_name": str(top["display_name"]).strip(),
        "score": round(float(s1), 4),
        "margin": round(margin, 4),
    }


def apply_auto_accept(payload: dict[str, Any], settings: Settings) -> list[str]:
    """Подтвердить в match-payload голоса, прошедшие правило. Вернуть принятые метки. Подтверждённые человеком не трогает."""
    matches = payload.get("matches") or {}
    candidates = payload.get("profile_candidates") or {}
    aliases = payload.get("voice_aliases") or {}
    accepted: list[str] = []
    checked: list[str] = []
    for label, row in matches.items():
        if not isinstance(row, dict) or (row.get("status") or "new") != "new":
            continue
        cands = list(candidates.get(label) or [])
        decision = auto_accept_decision(cands, settings, aliases)
        top = cands[0] if cands else {}
        checked.append(f"{label}:{top.get('display_name') or '-'}:{top.get('cosine')}:{'да' if decision else 'нет'}")
        if decision is None:
            continue
        row["status"] = "confirmed"
        row["profile_id"] = decision["profile_id"]
        row["profile_name"] = decision["profile_name"]
        row["score"] = decision["score"]
        row["auto"] = {
            "score": decision["score"],
            "margin": decision["margin"],
            "rule": f"cosine ≥ {settings.voice_auto_accept_min} и отрыв ≥ {settings.voice_auto_accept_margin}",
            "at": _now(),
        }
        # Имя подставлено, но голос ждёт кнопки человека на странице «Голоса» (решение 2026-10-15): до неё созвон не «готов».
        row["needs_human"] = True
        payload.setdefault("display_names", {})[label] = decision["profile_name"]
        accepted.append(label)
    if checked:
        # разбор каждого голоса пишется всегда: иначе «ничего не принято» не отличить от «не запускалось»
        log_event(logger, "speakers.auto_checked", checked=";".join(checked), accepted=",".join(accepted))
    return accepted


def auto_accept_lab_voices(settings: Settings, artifact: Any, run_id: str) -> list[str]:
    """Автопринятие в lab ``voice/match.json`` прогона (до публикации — имена сразу попадают в стенограмму)."""
    from app.app.services.artifact_lab import resolve_run

    path = resolve_run(settings, artifact, run_id).resolve("voice/match.json")
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    accepted = apply_auto_accept(payload, settings)
    if accepted:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log_event(
            logger,
            "speakers.auto_accepted",
            artifact=artifact.artifact_id,
            labels=",".join(accepted),
            names=",".join(payload["matches"][lbl]["profile_name"] for lbl in accepted),
        )
    return accepted


def auto_labels(payload: dict[str, Any] | None) -> set[str]:
    """Метки, принятые автоматически (для них не делаем enroll / sync в global при подтверждении)."""
    matches = (payload or {}).get("matches") or {}
    return {label for label, row in matches.items() if isinstance(row, dict) and row.get("auto")}


def unnamed_labels(payload: dict[str, Any] | None) -> list[str]:
    """Канонические метки голосов без подтверждённого имени."""
    payload = payload or {}
    merge_map = payload.get("merge_map") or {}
    canon = sorted({merge_map.get(lbl, lbl) for lbl in (payload.get("matches") or {})})
    out = []
    for label in canon:
        row = (payload.get("matches") or {}).get(label) or {}
        named = (row.get("status") in ("confirmed", "merged_duplicate")) and str(row.get("profile_name") or "").strip()
        if not named:
            out.append(label)
    return out


def _hint_context(turns: list, labels: list[str]) -> dict[str, list[str]]:
    from app.app.services.transcript_turns import format_timecode

    speech = [t for t in turns if t.is_speech]
    ctx: dict[str, list[str]] = {label: [] for label in labels}
    for pos, turn in enumerate(speech):
        if turn.speaker not in ctx or len(ctx[turn.speaker]) >= HINT_TURNS_PER_LABEL * 2:
            continue
        if pos > 0:
            prev = speech[pos - 1]
            ctx[turn.speaker].append(f"  перед ней [{format_timecode(prev.start_sec)}] {prev.speaker}: {prev.text[:HINT_TURN_CHARS]}")
        ctx[turn.speaker].append(f"  [{format_timecode(turn.start_sec)}] {turn.speaker}: {turn.text[:HINT_TURN_CHARS]}")
    return ctx


def suggest_names(settings: Settings, turns: list, labels: list[str], known_names: list[str]) -> dict[str, dict[str, Any]]:
    """Подсказки имён для неизвестных меток. Вызывать без открытой транзакции БД (LLM отвечает секунды)."""
    from app.app.services.llm import chat_completion_no_think, strip_json_fence
    from app.app.services.prompts import load_prompt_block

    ctx = {label: lines for label, lines in _hint_context(turns, labels).items() if lines}
    if not ctx:
        return {}
    user = "Известные участники: " + (", ".join(sorted(set(known_names))) or "нет") + "\n\n"
    user += "\n\n".join(f"Метка {label}:\n" + "\n".join(lines) for label, lines in ctx.items())
    reply = chat_completion_no_think(settings, system=load_prompt_block(PROMPT_PATH), user=user, max_tokens=1024)
    try:
        data = json.loads(strip_json_fence(reply))
    except json.JSONDecodeError:
        log_event(logger, "speakers.hints_unparsed", labels=",".join(ctx))
        return {}
    hints: dict[str, dict[str, Any]] = {}
    for row in data.get("speakers") or []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "").strip()
        name = str(row.get("name") or "").strip()
        try:
            confidence = float(row.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if label not in ctx or not name or confidence < HINT_MIN_CONFIDENCE or PLACEHOLDER_LABEL.match(name):
            continue
        hints[label] = {
            "name": name[:80],
            "quote": str(row.get("quote") or "")[:200],
            "confidence": round(confidence, 2),
            "prompt_version": PROMPT_VERSION,
            "at": _now(),
        }
    return hints


def refresh_speaker_hints(db: Any, settings: Settings, artifact: Any) -> int:
    """Подсказки имён для неназванных голосов созвона: собрать контекст, освободить БД, спросить модель, записать, пересобрать карточки."""
    from sqlalchemy import select

    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity
    from app.app.services.speaker_profiles import match_json_path, rebuild_match_report_html
    from app.app.services.transcript_turns import load_turns

    if getattr(artifact, "file_type", "") != "audio":
        return 0
    project_id, artifact_id = artifact.project_id, artifact.artifact_id
    match_path = match_json_path(project_id, artifact_id)
    if not match_path.is_file():
        return 0
    payload = json.loads(match_path.read_text(encoding="utf-8"))
    labels = [lbl for lbl in unnamed_labels(payload) if not ((payload.get("matches") or {}).get(lbl) or {}).get("llm_hint")]
    if not labels:
        return 0
    turns = load_turns(settings, artifact)
    known = [str(row.get("profile_name")) for row in (payload.get("matches") or {}).values()
             if isinstance(row, dict) and row.get("profile_name") and row.get("status") in ("confirmed", "merged_duplicate")]
    for (value_json,) in db.execute(
        select(Entity.value_json).join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == project_id, Entity.entity_type == "participant", Entity.is_active.is_(True))
    ).all():
        try:
            name = str((json.loads(value_json) or {}).get("name") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            continue
        if name:
            known.append(name)
    db.commit()  # DECISIONS 2026-09-21: соединение БД не держим на время ответа LLM
    hints = suggest_names(settings, turns, labels, known)
    written = write_hints(match_path, hints)
    if written:
        rebuild_match_report_html(db, project_id=project_id, artifact_id=artifact_id)
    log_event(logger, "speakers.hints", artifact=artifact_id, asked=",".join(labels), labels=",".join(hints),
              names=",".join(h["name"] for h in hints.values()), written=written)
    return written


def write_hints(match_path: Path, hints: dict[str, dict[str, Any]]) -> int:
    """Записать подсказки в боевой match JSON (``matches[label].llm_hint``). Имена не ставит."""
    if not hints or not match_path.is_file():
        return 0
    payload = json.loads(match_path.read_text(encoding="utf-8"))
    matches = payload.get("matches") or {}
    written = 0
    for label, hint in hints.items():
        row = matches.get(label)
        if isinstance(row, dict) and (row.get("status") or "new") not in ("confirmed", "merged_duplicate"):
            row["llm_hint"] = hint
            written += 1
    if written:
        match_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return written
