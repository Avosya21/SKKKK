"""Техническое задание проекта: пересборка `current_tz.md` из материалов (быстрый путь top-K и полный map/reduce), версии, оценка объёма, человекочитаемые ссылки на артефакты."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.entity import Changelog, Chunk, Entity, UserNote
from app.app.services.graph import _cosine
from app.app.services.llm import chat_completion_no_think
from app.app.services.ollama_client import embed_texts
from app.app.services.plain_text import clean_summary_output
from app.app.services.retrieval import artifact_sort_key, format_entity_line, search_entities
from app.app.services.storage import artifact_knowledge_text, parsed_dir, project_root, structured_dir
from app.app.services.vector_index import (
    deserialize_float32,
    load_entity_vectors,
    load_sqlite_vec,
    search_vectors,
)

logger = logging.getLogger("skkkk.tz")
ROOT = Path(__file__).resolve().parents[3]
PROMPT_PATH = ROOT / "docs" / "prompts" / "rebuild_tz.md"
MAP_CHUNK_CHARS = 6000
# qwen в нашей сборке почти весь бюджет тратит на reasoning (reasoning_tokens≈
# 2400–3000 на вызов), поэтому 1200 токенов оставляло ноль под контент — черновики
# выходили пустыми. 4096 хватает: reasoning + ~150–200 токенов заметок.
MAP_MAX_TOKENS = 4096
# Слияние черновиков — НЕ обедняющий пересказ (иначе финальный reduce не увидит
# факты). Бюджет вывода крупный, чтобы reasoning не съедал весь максимум и не
# возвращал пустой черновик; маленькие группы — чтобы merge не додумывал и не
# обобщал. Прогресс контролируем в _collapse_drafts по оценке токенов.
MERGE_MAX_TOKENS = 16000
REDUCE_MAX_TOKENS = 16000
# Бюджеты в оценённых токенах. Оценка len//2 занижает реальные токены:
# на реальных данных qwen даёт ~2.6 симв./токен и больше (проверено на живых
# вызовах), плюс overhead системы/токенизации. Поэтому держим вход reduce не выше
# ~10k оценённых (≈20k символов → ~14–15k реальных токенов); вместе с reduce_sys
# (~4k real) и выводом это остаётся в n_ctx=32000 с запасом.
REDUCE_INPUT_BUDGET_TOKENS = 10000
# Раньше 4000: при среднем черновике ~1000 оцен. токенов группы часто выходили
# из одного черновика — модель просто переписывала его обратно, не сливая ни с
# чем (проверено на живом прогоне: одинаковые chars/completion_tokens/prompt_tokens
# на одной и той же позиции по несколько уровней подряд). 12000 даёт реальную
# группу из 8-12 черновиков за вызов; single-item группы всё равно не отправляем
# на LLM (см. ниже) — это подстраховка, а не единственный фикс.
MERGE_INPUT_BUDGET_TOKENS = 12000
MAX_MERGE_LEVELS = 10
# Pre-flight полного map/reduce (пункт 2.4). Слияние удерживает ~89% объёма за уровень
# (проект 18: 66k -> 58.8k), поэтому при большом числе черновиков оно не сходится к бюджету reduce.
# Средний черновик ≈ 1000 оценённых токенов; предел — втрое больше бюджета reduce.
MAP_DRAFT_EST_TOKENS = 1000
FULL_MAP_MAX_DRAFT_TOKENS = 3 * REDUCE_INPUT_BUDGET_TOKENS


class TzTooLargeError(RuntimeError):
    """Проект слишком большой для полного map/reduce — используйте rebuild_tz_fast."""


def preflight_full_rebuild(map_chunk_count: int) -> None:
    est = map_chunk_count * MAP_DRAFT_EST_TOKENS
    if est > FULL_MAP_MAX_DRAFT_TOKENS:
        raise TzTooLargeError(
            f"Полный map/reduce не сойдётся: {map_chunk_count} фрагментов (~{est} оценённых токенов черновиков, "
            f"предел {FULL_MAP_MAX_DRAFT_TOKENS}). Используйте быструю сборку (rebuild_tz_fast)."
        )
TZ_ENTITY_TYPES = frozenset({"decision", "task"})

# --- rebuild_tz_fast: top-K векторный свод вместо полного map/reduce ---
# Полный map/reduce упирается в n_ctx=32000 на проектах с большим объёмом
# уникального (низкоизбыточного) контента — слияние черновиков не может сжать
# материал ниже определённого объёма (проверено: 66k -> 58.8k за уровень,
# ~89% удержания, дальше сблизиться нечем). Fast-путь не пытается охватить всё:
# он берёт top-K релевантных фрагментов через векторный поиск + MMR-отбор для
# разнообразия (не только самые похожие друг на друга) и собирает резюме одним
# reduce-вызовом. Ограничен по конструкции — не зависит от размера проекта,
# поэтому годится как безопасный дефолт для автопересборки.
# Реальный прогон на project 18 (578 чанков, 321 активная decision/task
# сущность) занял всего prompt+completion ≈ 8900 токенов из n_ctx=32000 —
# огромный запас. Подняли K в ~1.5-1.7 раза против первой версии: не пытаемся
# взять ВСЕ 321 сущности (это почти весь бюджет и риск притащить устаревшие
# дубли решений без предварительной чистки, как в map/reduce) — берём больше
# top-K с MMR, оставляя запас под системный промпт и вывод.
FAST_HITS_PER_QUERY = 10
FAST_ENTS_PER_QUERY = 9
FAST_MAX_CHUNKS = 36
FAST_QUOTA_MAX_EXTRA = 12  # добавка по квоте артефактов (2.3c), чтобы не раздувать контекст
FAST_MAX_ENTS = 36
FAST_MMR_LAMBDA = 0.65
FAST_CHUNK_TRIM = 900
FAST_REDUCE_MAX_TOKENS = 20000
# Общие для любого проекта внедрения 1С — без привязки к тематике конкретного
# заказчика (раньше здесь были захардкожены термины одного проекта вроде
# "профнастил"/"эквайринг", что не давало пользы на других проектах).
FAST_QUERIES = [
    "Цель проекта, заказчик, периметр работ, конфигурация 1С и внешние интеграции",
    "Принятые ключевые технические решения: обмен данными, объекты, реквизиты, расписание, поля",
    "Что уже сделано, выполнено, настроено, запущено, подключено",
    "Актуальные задачи, сроки, исполнители, план работ, этапы",
    "Открытые вопросы, риски, проблемы, ограничения, доступы, права, нерешённое",
    "Конкретные объекты, термины, номенклатура и реквизиты, обсуждаемые в проекте",
    "Технические детали и уточнения по интеграции, обмену данными и настройкам",
]
_ART_ID_IN_PARENS = re.compile(r"\((art_\d{4}-\d{2}-\d{2}_\d{6}_[a-f0-9]+)\)", re.IGNORECASE)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 2 + 1)


def _chat_nonempty(
    settings: Settings,
    *,
    system: str,
    user: str,
    max_tokens: int,
) -> str:
    """chat_completion_no_think с одним ретраем, если модель вернула пустой текст."""
    text = chat_completion_no_think(
        settings,
        system=system,
        user=user,
        max_tokens=max_tokens,
        temperature=0.0,
    ).strip()
    if text:
        return text
    retry_max = min(max_tokens + 4096, 20000)
    log_event(logger, "tz.empty_retry", max_tokens=retry_max)
    text = chat_completion_no_think(
        settings,
        system=system,
        user=user,
        max_tokens=retry_max,
        temperature=0.0,
    ).strip()
    if not text:
        raise RuntimeError(
            f"LLM вернул пустой текст дважды (max_tokens={retry_max}); "
            f"черновик нельзя собрать — факты фрагмента теряются."
        )
    return text


def _joined_drafts(drafts: list[str]) -> str:
    return "\n\n---\n\n".join(drafts)


def _artifact_filename_map(db: Session, project_id: int) -> dict[str, str]:
    rows = db.scalars(select(Artifact).where(Artifact.project_id == project_id)).all()
    return {a.artifact_id: a.original_filename for a in rows}


def _humanize_artifact_refs(text: str, id_to_name: dict[str, str]) -> str:
    """Заменить (art_…) на (имя файла) в готовой выжимке."""

    def repl(match: re.Match[str]) -> str:
        name = id_to_name.get(match.group(1))
        return f"({name})" if name else match.group(0)

    return _ART_ID_IN_PARENS.sub(repl, text)


def _source_label(art: Artifact, detail: str) -> str:
    return f"{art.original_filename} ({detail})"


def tz_paths(settings: Settings, project_id: int) -> tuple[Path, Path]:
    base = project_root(settings, project_id) / "project_state"
    base.mkdir(parents=True, exist_ok=True)
    versions = base / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    return base / "current_tz.md", versions


def _parse_prompts() -> tuple[str, str, str, str, str, str]:
    """rebuild_tz.md: map-sys, map-user, merge-sys, merge-user, reduce-sys, reduce-user (6 code blocks)."""
    blocks = _read_prompt().split("```")
    if len(blocks) < 12:
        raise RuntimeError(f"{PROMPT_PATH}: expected 6 prompt blocks")
    return tuple(blocks[i].strip() for i in (1, 3, 5, 7, 9, 11))  # type: ignore[return-value]


def _read_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _load_reduce_system() -> str:
    """Reduce-системный блок для dry-run / отладки."""
    _sett, _mapu, _merm, _meru, reduce_sys, _reduce_u = _parse_prompts()
    return reduce_sys


def _split_oversized(title: str, body: str, max_chars: int) -> list[tuple[str, str]]:
    if len(body) <= max_chars:
        return [(title, body)]
    paras = [p.strip() for p in re.split(r"\n{2,}", body) if p.strip()]
    if not paras:
        paras = [body]
    normalized: list[str] = []
    for para in paras:
        if len(para) <= max_chars:
            normalized.append(para)
        else:
            normalized.extend(para[i : i + max_chars] for i in range(0, len(para), max_chars))
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    size = 0
    part = 0
    for para in normalized:
        extra = len(para) + (2 if buf else 0)
        if buf and size + extra > max_chars:
            part += 1
            out.append((f"{title} (ч.{part})", "\n\n".join(buf)))
            buf, size = [], 0
        buf.append(para)
        size += extra
    if buf:
        part += 1
        suffix = f" (ч.{part})" if part > 1 or len(out) else ""
        out.append((f"{title}{suffix}", "\n\n".join(buf)))
    return out


def _pack_map_chunks(parts: list[tuple[str, str]], *, max_chars: int = MAP_CHUNK_CHARS) -> list[str]:
    """Собрать фрагменты для map-прохода (~6000 символов)."""
    expanded: list[tuple[str, str]] = []
    for title, body in parts:
        expanded.extend(_split_oversized(title, body.strip(), max_chars))

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for title, body in expanded:
        block = f"## {title}\n\n{body}\n"
        if current and size + len(block) > max_chars:
            chunks.append("\n".join(current))
            current, size = [], 0
        if len(block) > max_chars:
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.append(block)
            continue
        current.append(block)
        size += len(block)
    if current:
        chunks.append("\n".join(current))
    return [c for c in chunks if c.strip()]


def _collect_project_material(db: Session, settings: Settings, project_id: int) -> list[tuple[str, str]]:
    """Материалы для ТЗ: lab ``_final.txt`` для аудио, чанки для документов, сущности, заметки."""
    parts: list[tuple[str, str]] = []

    artifacts = db.scalars(
        select(Artifact)
        .where(Artifact.project_id == project_id, Artifact.status == "done")
    ).all()
    artifacts.sort(key=artifact_sort_key)

    for art in artifacts:
        chunks = db.scalars(
            select(Chunk)
            .where(Chunk.artifact_id == art.id)
            .order_by(Chunk.chunk_index)
        ).all()
        if art.file_type == "audio":
            lab_text = artifact_knowledge_text(settings, art)
            if lab_text:
                parts.append(
                    (
                        _source_label(art, f"стенограмма lab, вес {art.priority_weight}"),
                        lab_text,
                    )
                )
            elif chunks:
                lines = [
                    f"[{c.speaker}] {c.text.strip()}" if c.speaker else c.text.strip() for c in chunks
                ]
                parts.append(
                    (
                        _source_label(art, f"индекс, {len(chunks)} чанков, вес {art.priority_weight}"),
                        "\n\n".join(lines),
                    )
                )
        elif chunks:
            lines = [
                f"[{c.speaker}] {c.text.strip()}" if c.speaker else c.text.strip() for c in chunks
            ]
            parts.append(
                (
                    _source_label(art, f"индекс, {len(chunks)} чанков, вес {art.priority_weight}"),
                    "\n\n".join(lines),
                )
            )
        elif art.transcription_text and art.transcription_text.strip():
            label = _source_label(art, f"текст, без индекса, вес {art.priority_weight}")
            parts.append((label, art.transcription_text.strip()))

        if art.file_type == "audio":
            summary_path = parsed_dir(settings, art.project_id, art.source_type) / f"{art.artifact_id}_call_summary.md"
            if summary_path.is_file():
                summary = summary_path.read_text(encoding="utf-8").strip()
                if summary:
                    parts.append((_source_label(art, "выжимка созвона"), summary))

    entities = db.scalars(
        select(Entity)
        .join(Artifact, Artifact.id == Entity.artifact_id)
        .where(
            Artifact.project_id == project_id,
            Entity.is_active.is_(True),
            Entity.entity_type.in_(TZ_ENTITY_TYPES),
        )
        .order_by(Entity.id)
    ).all()
    if entities:
        ent_lines = [f"- {format_entity_line(e.entity_type, e.value_json, e.artifact)}" for e in entities]
        parts.append(("Сущности проекта (БД)", "\n".join(ent_lines)))

    notes = db.scalars(
        select(UserNote).where(UserNote.project_id == project_id).order_by(UserNote.id)
    ).all()
    if notes:
        note_lines = [f"- (note #{n.id}) {n.text.strip()}" for n in notes if n.text.strip()]
        if note_lines:
            parts.append(("Заметки пользователей", "\n".join(note_lines)))

    # Хронологией, человеческими строками (CONFLICT-AUTONOMY-PLAN этап 5): «в силе / заменено / основание».
    from app.app.services.conflict_context import conflict_lines

    resolved_lines, open_lines = conflict_lines(db, project_id, limit=60)
    if resolved_lines:
        parts.append(("Изменения решений (что в силе и что заменено)", "\n".join(resolved_lines)))
    if open_lines:
        parts.append(("Нерешённые противоречия", "\n".join(open_lines)))
    from app.app.services.conflict_context import open_question_lines

    questions = open_question_lines(db, project_id, limit=60)
    if questions:
        parts.append(("Вопросы к заказчику (варианты без выбора)", "\n".join(questions)))

    return parts


def _collect_context(db: Session, settings: Settings, project_id: int) -> str:
    """Плоский контекст для dry-run / отладки."""
    parts = _collect_project_material(db, settings, project_id)
    return "\n\n".join(f"## {title}\n\n{body}" for title, body in parts)


def _pack_draft_groups(
    drafts: list[str],
    *,
    budget_tokens: int = MERGE_INPUT_BUDGET_TOKENS,
) -> list[list[str]]:
    """Разбить черновики на группы, каждая помещается в один merge-вызов."""
    groups: list[list[str]] = []
    current: list[str] = []
    size = 0
    for draft in drafts:
        tok = _estimate_tokens(draft)
        if current and size + tok > budget_tokens:
            groups.append(current)
            current, size = [], 0
        current.append(draft)
        size += tok
    if current:
        groups.append(current)
    return groups


def _collapse_drafts(
    settings: Settings,
    *,
    merge_sys: str,
    merge_user_tmpl: str,
    drafts: list[str],
) -> list[str]:
    """Иерархически слить черновики, пока всё не уложится в бюджет финального reduce."""
    level = 0
    prev_total = _estimate_tokens(_joined_drafts(drafts))
    while prev_total > REDUCE_INPUT_BUDGET_TOKENS:
        groups = _pack_draft_groups(drafts)
        if len(groups) <= 1:
            raise RuntimeError(
                f"merge: итоговый черновик {_estimate_tokens(_joined_drafts(drafts))} "
                f"токенов не умещается в reduce-бюджет {REDUCE_INPUT_BUDGET_TOKENS}"
            )
        level += 1
        if level > MAX_MERGE_LEVELS:
            raise RuntimeError(
                f"merge: уровень {level} > MAX_MERGE_LEVELS={MAX_MERGE_LEVELS}, "
                f"размер черновиков не сходится"
            )
        merged: list[str] = []
        calls_made = 0
        for gi, group in enumerate(groups):
            log_event(
                logger,
                "tz.merge",
                merge_level=level,
                group=gi + 1,
                total=len(groups),
                drafts=len(group),
            )
            if len(group) == 1:
                # Группа из одного черновика: слать её в LLM бессмысленно — "слияние"
                # одного элемента с собой лишь переписывает его же, тратя вызов впустую
                # (это и вызывало пробуксовку: один и тот же черновик слово в слово
                # повторялся уровень за уровнем). Черновик уже в нужном формате — просто
                # проносим его дальше без изменений.
                merged.append(group[0])
                continue
            calls_made += 1
            user = merge_user_tmpl.format(drafts=_joined_drafts(group))
            merged.append(
                _chat_nonempty(
                    settings,
                    system=merge_sys,
                    user=user,
                    max_tokens=MERGE_MAX_TOKENS,
                )
            )
        merged_total = _estimate_tokens(_joined_drafts(merged))
        log_event(
            logger,
            "tz.merge.level_done",
            merge_level=level,
            groups=len(groups),
            calls=calls_made,
            passthrough=len(groups) - calls_made,
            prev_total=prev_total,
            merged_total=merged_total,
        )
        # Без прогресса дальше не сливаем: повторные проходы только раздувают
        # вызовы. Если в бюджет всё ещё не уместились — это явная ошибка, которую
        # надо видеть, а не замалчивать.
        if merged_total >= prev_total:
            raise RuntimeError(
                f"merge: уровень {level} не уменьшает объём "
                f"({prev_total} -> {merged_total}), группы не сходятся"
            )
        drafts = merged
        prev_total = merged_total
    return drafts


def _build_tz_body(
    settings: Settings,
    *,
    map_chunks: list[str],
) -> str:
    map_sys, map_user_tmpl, merge_sys, merge_user_tmpl, reduce_sys, reduce_user_tmpl = _parse_prompts()

    if len(map_chunks) == 1:
        drafts = map_chunks
    else:
        drafts = []
        for i, chunk in enumerate(map_chunks):
            user = map_user_tmpl.format(i=i + 1, n=len(map_chunks), chunk_text=chunk)
            log_event(logger, "tz.map", chunk=i + 1, total=len(map_chunks), chars=len(chunk))
            drafts.append(
                _chat_nonempty(
                    settings,
                    system=map_sys,
                    user=user,
                    max_tokens=MAP_MAX_TOKENS,
                )
            )

    drafts = _collapse_drafts(settings, merge_sys=merge_sys, merge_user_tmpl=merge_user_tmpl, drafts=drafts)
    if drafts:
        est = _estimate_tokens(_joined_drafts(drafts))
        log_event(logger, "tz.reduce", drafts=len(drafts), est_tokens=est)

    reduce_user = reduce_user_tmpl.format(drafts=_joined_drafts(drafts) if drafts else "")
    return chat_completion_no_think(
        settings,
        system=reduce_sys,
        user=reduce_user,
        max_tokens=REDUCE_MAX_TOKENS,
        temperature=0.0,
    )


def rebuild_tz(db: Session, settings: Settings, project_id: int, *, manual: bool = False, summary: str = "") -> Path:
    chunk_count = db.scalar(
        select(func.count())
        .select_from(Chunk)
        .where(Chunk.project_id == project_id)
    ) or 0
    log_event(
        logger,
        "tz.rebuild.start",
        project_id=project_id,
        manual=manual,
        dry_run=settings.dry_run,
        indexed_chunks=chunk_count,
    )
    current, versions = tz_paths(settings, project_id)
    prev = current.read_text(encoding="utf-8") if current.is_file() else ""

    material = _collect_project_material(db, settings, project_id)
    if not material:
        log_event(logger, "tz.rebuild.empty", project_id=project_id)
        body = "# Итоговая выжимка по проекту\n\nМатериалов для сборки пока нет (нет готовых артефактов с индексом).\n"
    elif settings.dry_run:
        context = _collect_context(db, settings, project_id)
        body = f"# Итоговая выжимка по проекту (dry run)\n\n{context[:8000]}\n"
    else:
        map_chunks = _pack_map_chunks(material)
        log_event(
            logger,
            "tz.rebuild.material",
            project_id=project_id,
            sections=len(material),
            map_chunks=len(map_chunks),
            indexed_chunks=chunk_count,
        )
        preflight_full_rebuild(len(map_chunks))
        # Закрыть транзакцию: ответ модели идёт десятки секунд, а соединение из пула всё это время было бы занято.
        db.commit()
        body = _build_tz_body(settings, map_chunks=map_chunks)

    from app.app.services.plain_text import clean_summary_output

    body = clean_summary_output(body)
    body = _humanize_artifact_refs(body, _artifact_filename_map(db, project_id))
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    if current.is_file() and prev.strip():
        archive = versions / f"current_tz_{stamp}.md"
        archive.write_text(prev, encoding="utf-8")
    current.write_text(body.strip() + "\n", encoding="utf-8")
    arts = db.scalars(select(Artifact.artifact_id).where(Artifact.project_id == project_id)).all()
    db.add(
        Changelog(
            project_id=project_id,
            version_label=stamp,
            artifact_ids_json=str(list(arts)),
            summary_of_changes=summary or ("manual rebuild" if manual else "auto rebuild"),
            tz_path=str(current),
        )
    )
    db.commit()
    log_event(
        logger,
        "tz.rebuild.done",
        project_id=project_id,
        version=stamp,
        chars=len(body),
        path=current.name,
    )
    return current


def _fast_mmr_select(
    ids: list[int],
    rel: dict[int, float],
    vecs: dict[int, list[float]],
    k: int,
    lam: float = FAST_MMR_LAMBDA,
) -> list[int]:
    """Greedy MMR: релевантность минус штраф за близость к уже выбранным.

    Без этого top-K по похожести на запрос часто возвращает почти дублирующие
    друг друга фрагменты (все про одну и ту же тему) и теряет фокус на других —
    ровно то, что раньше приводило к прямым противоречиям в резюме.
    """
    pool = [i for i in ids if i in rel]
    if not pool or k <= 0:
        return []
    if not vecs:
        return sorted(pool, key=lambda i: rel[i], reverse=True)[:k]
    chosen: list[int] = []
    chosen_vecs: list[list[float]] = []
    while len(chosen) < k and pool:
        best_i, best_score, best_vec = None, -1e9, None
        for i in pool:
            v = vecs.get(i)
            if v is None:
                continue
            d = max((_cosine(v, c) for c in chosen_vecs), default=0.0)
            s = lam * rel.get(i, 0.0) - (1 - lam) * d
            if s > best_score:
                best_i, best_score, best_vec = i, s, v
        if best_i is None:
            break
        chosen.append(best_i)
        chosen_vecs.append(best_vec)
        pool = [x for x in pool if x != best_i]
    return chosen


def _trim_chunk_lines(chunk_text: str, limit: int) -> str:
    """Урезать фрагмент до ``limit`` знаков целыми строками (у окна реплик строка = «[таймкод] Имя: текст»)."""
    kept: list[str] = []
    size = 0
    for line in chunk_text.splitlines():
        if kept and size + len(line) + 1 > limit:
            break
        kept.append(line)
        size += len(line) + 1
    body = "\n".join(kept)
    if len(body) > limit:
        body = body[:limit].rstrip()
    return body.rstrip() + " …"


def _fast_chunk_embeddings(db: Session, ids: list[int]) -> dict[int, list[float]]:
    """Векторы чанков прямо из vec0 (BLOB/hex), без переэмбеддинга."""
    conn = db.connection()
    load_sqlite_vec(conn.connection.dbapi_connection)
    out: dict[int, list[float]] = {}
    for start in range(0, len(ids), 100):
        batch = ids[start : start + 100]
        if not batch:
            break
        rows = conn.execute(
            text("SELECT chunk_id, embedding FROM chunk_vectors WHERE chunk_id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": batch},
        ).mappings().all()
        for row in rows:
            emb = row["embedding"]
            raw = bytes.fromhex(emb[2:-1]) if isinstance(emb, str) and emb.startswith("X") else emb
            vec = deserialize_float32(raw)
            if len(vec) == 1024:
                out[int(row["chunk_id"])] = vec
    return out


def _fast_quota_chunks(db: Session, project_id: int, chunk_ids: list[int], chunk_seen: dict[int, dict]) -> list[int]:
    """Квота по артефактам (пункт 2.3c): каждый готовый артефакт проекта даёт хотя бы один фрагмент.
    Кладём в chunk_seen первый фрагмент артефакта, если MMR его не выбрал; не больше FAST_QUOTA_MAX_EXTRA."""
    covered = {chunk_seen[cid]["artifact_id"] for cid in chunk_ids if cid in chunk_seen}
    rows = db.execute(
        select(Chunk.id, Chunk.artifact_id, Chunk.chunk_index, Chunk.text)
        .join(Artifact, Artifact.id == Chunk.artifact_id)
        .where(Chunk.project_id == project_id, Artifact.status == "done")
        .order_by(Chunk.artifact_id, Chunk.chunk_index)
    ).all()
    first: dict[int, tuple] = {}
    for row in rows:
        first.setdefault(row.artifact_id, row)
    extra: list[int] = []
    for art_id, row in first.items():
        if art_id in covered or len(extra) >= FAST_QUOTA_MAX_EXTRA:
            continue
        chunk_seen[row.id] = {
            "chunk_id": row.id,
            "artifact_id": row.artifact_id,
            "chunk_index": row.chunk_index,
            "text": row.text,
            "score": 0.0,
        }
        extra.append(row.id)
    return extra


def build_tz_fast_body(db: Session, settings: Settings, project_id: int) -> tuple[str | None, dict]:
    """Только сборка текста (без записи файлов и БД): годится для eval на клиентских проектах.
    Возвращает (текст или None, статистика покрытия)."""
    map_sys, _mu, _ms, _meu, reduce_sys, reduce_user_tmpl = _parse_prompts()
    stats: dict = {}
    q_vectors = embed_texts(settings, FAST_QUERIES)

    chunk_seen: dict[int, dict] = {}
    ent_seen: dict[int, dict] = {}
    chunk_rel: dict[int, float] = {}
    ent_rel: dict[int, float] = {}
    for q_vec in q_vectors:
        for h in search_vectors(db, q_vec, project_id=project_id, limit=FAST_HITS_PER_QUERY):
            chunk_seen.setdefault(h["chunk_id"], h)
            chunk_rel[h["chunk_id"]] = max(chunk_rel.get(h["chunk_id"], 0.0), h.get("score") or 0.0)
        for e in search_entities(db, q_vec, project_id=project_id, limit=FAST_ENTS_PER_QUERY):
            ent_seen.setdefault(e["entity_id"], e)
            ent_rel[e["entity_id"]] = max(ent_rel.get(e["entity_id"], 0.0), e.get("score") or 0.0)
    log_event(logger, "tz.rebuild_fast.retrieval", project_id=project_id, chunks=len(chunk_seen), ents=len(ent_seen))

    chunk_vecs = _fast_chunk_embeddings(db, list(chunk_seen.keys()))
    ent_vecs = load_entity_vectors(db, list(ent_seen.keys()))
    chunk_ids = _fast_mmr_select(list(chunk_seen.keys()), chunk_rel, chunk_vecs, FAST_MAX_CHUNKS)
    ent_ids = _fast_mmr_select(list(ent_seen.keys()), ent_rel, ent_vecs, FAST_MAX_ENTS)
    quota_ids = _fast_quota_chunks(db, project_id, chunk_ids, chunk_seen)
    chunk_ids = chunk_ids + quota_ids
    hits = [chunk_seen[cid] for cid in chunk_ids if cid in chunk_seen]
    ents = [ent_seen[eid] for eid in ent_ids]
    total_arts = db.scalar(
        select(func.count(func.distinct(Chunk.artifact_id)))
        .join(Artifact, Artifact.id == Chunk.artifact_id)
        .where(Chunk.project_id == project_id, Artifact.status == "done")
    ) or 0
    stats.update(
        chunks_selected=len(hits),
        chunks_candidates=len(chunk_seen),
        quota_extra=len(quota_ids),
        entities_selected=len(ents),
        entities_candidates=len(ent_seen),
        artifacts_total=total_arts,
        artifacts_covered=len({h["artifact_id"] for h in hits}),
    )
    log_event(logger, "tz.rebuild_fast.coverage", project_id=project_id, **stats)

    arts: dict[int, Artifact] = {}
    for h in hits:
        arts.setdefault(h["artifact_id"], db.get(Artifact, h["artifact_id"]))
    rng = lambda a: (artifact_sort_key(a) if a is not None else (0, 0, 0, ""))  # noqa: E731
    hits.sort(key=lambda h: (rng(arts.get(h["artifact_id"])), h["chunk_index"]))

    notes = db.scalars(select(UserNote).where(UserNote.project_id == project_id).order_by(UserNote.id)).all()

    blocks: list[str] = []
    for h in hits:
        chunk_text = (h["text"] or "").strip()
        if not chunk_text:
            continue
        if len(chunk_text) > FAST_CHUNK_TRIM:
            chunk_text = _trim_chunk_lines(chunk_text, FAST_CHUNK_TRIM)
        art = arts.get(h["artifact_id"])
        units = h.get("unit_first")
        detail = (f"сообщения {units}–{h.get('unit_last')}" if units and h.get("unit_last") != units else f"сообщение {units}") if units \
            else f"фрагмент {h['chunk_index']}"
        title = _source_label(art, detail) if art else f"чанк {h['chunk_id']}"
        blocks.append(f"## {title}\n\n{chunk_text}")
    if ents:
        lines = [f"- {e.get('line') or '[' + e['entity_type'] + '] ' + e['text']}" for e in ents if e["text"]]
        if lines:
            blocks.append("## Сущности проекта (БД)\n\n" + "\n".join(lines))
    note_lines = [f"- (note #{n.id}) {n.text.strip()}" for n in notes if n.text.strip()]
    if note_lines:
        blocks.append("## Заметки пользователей\n\n" + "\n".join(note_lines))
    # Что чем заменено, что не решено, какие вопросы ждут заказчика (REQUIREMENTS-PLAN этап 10): без этого быстрая сборка
    # видела только найденные фрагменты и могла вернуть в ТЗ решение, от которого отказались.
    from app.app.services.conflict_context import conflict_lines, open_question_lines

    resolved_lines, open_lines = conflict_lines(db, project_id, limit=30)
    if resolved_lines:
        blocks.append("## Изменения решений (что в силе и что заменено)\n\n" + "\n".join(resolved_lines))
    if open_lines:
        blocks.append("## Нерешённые противоречия\n\n" + "\n".join(open_lines))
    questions = open_question_lines(db, project_id)
    if questions:
        blocks.append("## Вопросы к заказчику (варианты без выбора)\n\n" + "\n".join(questions))

    if not blocks:
        log_event(logger, "tz.rebuild_fast.empty", project_id=project_id)
        return None, stats  # заглушку «Материалов для сборки пока нет» пишет rebuild_tz_fast

    material = "\n\n".join(blocks)
    reduce_user = reduce_user_tmpl.format(drafts=material)

    # Закрыть транзакцию: ответ модели идёт десятки секунд, а соединение из пула всё это время было бы занято.
    db.commit()
    body = chat_completion_no_think(
        settings, system=reduce_sys, user=reduce_user, max_tokens=FAST_REDUCE_MAX_TOKENS, temperature=0.0
    ).strip()
    if not body:
        raise RuntimeError("rebuild_tz_fast: reduce вернул пустой текст — выжимку собрать не удалось")

    body = clean_summary_output(body)
    body = _humanize_artifact_refs(body, _artifact_filename_map(db, project_id))
    return body.strip(), stats


def rebuild_tz_fast(
    db: Session, settings: Settings, project_id: int, *, manual: bool = False, summary: str = ""
) -> Path:
    """Top-K векторный свод: retrieval (чанки+сущности, с MMR на разнообразие)
    + заметки -> один reduce-вызов. Не читает весь проект, поэтому не зависит
    от его размера — используется как дефолт для автопересборки (см. модуль-
    ный докстринг у FAST_* констант) и как более быстрая альтернатива ручному
    rebuild_tz для больших проектов, где полный map/reduce не сходится."""
    log_event(logger, "tz.rebuild_fast.start", project_id=project_id, manual=manual)
    current, versions = tz_paths(settings, project_id)
    prev = current.read_text(encoding="utf-8") if current.is_file() else ""
    body, _stats = build_tz_fast_body(db, settings, project_id)
    if body is None:
        current.write_text(
            "# Итоговая выжимка по проекту" + chr(10) * 2 + "Материалов для сборки пока нет (нет готовых артефактов с индексом)." + chr(10),
            encoding="utf-8",
        )
        return current
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    if current.is_file() and prev.strip():
        (versions / f"current_tz_{stamp}.md").write_text(prev, encoding="utf-8")
    current.write_text(body.strip() + "\n", encoding="utf-8")
    arts_all = db.scalars(select(Artifact.artifact_id).where(Artifact.project_id == project_id)).all()
    db.add(
        Changelog(
            project_id=project_id,
            version_label=stamp,
            artifact_ids_json=str(list(arts_all)),
            summary_of_changes=summary or ("manual fast rebuild" if manual else "auto fast rebuild"),
            tz_path=str(current),
        )
    )
    db.commit()
    log_event(logger, "tz.rebuild_fast.done", project_id=project_id, version=stamp, chars=len(body))
    return current


def _other_pending_jobs(db: Session, artifact: Artifact) -> int:
    """Другие необработанные задания того же проекта (для пакетной загрузки: пересобираем один раз в конце)."""
    from app.app.models.job import Job

    return db.scalar(
        select(func.count())
        .select_from(Job)
        .join(Artifact, Artifact.id == Job.artifact_id)
        .where(
            Artifact.project_id == artifact.project_id,
            Artifact.id != artifact.id,
            Job.kind == "process_artifact",
            Job.status.in_(("queued", "running")),
        )
    ) or 0


def summary_jobs_queued(db: Session, project_id: int) -> int:
    """Задачи итоговой выжимки проекта, ждущие в очереди за проверками материалов (``auto_review``, scope ``summary``)."""
    from app.app.models.job import Job

    return db.scalar(
        select(func.count())
        .select_from(Job)
        .join(Artifact, Artifact.id == Job.artifact_id)
        .where(
            Artifact.project_id == project_id,
            Job.status == "queued",
            Job.payload_json.like('%"scope": "summary"%'),
        )
    ) or 0


def maybe_auto_rebuild_tz(db: Session, settings: Settings, artifact: Artifact) -> None:
    """Автопересборка выжимки после нового материала (и после упавшего последнего файла партии).

    С автопроверкой материал ставит выжимку задачей очереди за своими проверками — она соберётся, когда актуальность и
    конфликты уже выключат заменённое (решение владельца 2026-09-29). Собранная здесь сохранила бы устаревшее: проверки
    идут позже, в очереди после расшифровок. Сразу — только без автопроверки: документ — в конце партии, созвон — каждый
    N-й готовый материал проекта.
    """
    from app.app.services.project_tasks import TASK_TZ_REBUILD, get_running_task, track_project_task

    waiting = summary_jobs_queued(db, artifact.project_id)
    if waiting:
        log_event(
            logger,
            "tz.rebuild.skipped",
            project_id=artifact.project_id,
            artifact=artifact.artifact_id,
            reason="after_checks",
            summary_jobs=waiting,
        )
        return

    if get_running_task(db, task_type=TASK_TZ_REBUILD, project_id=artifact.project_id):
        log_event(
            logger,
            "tz.rebuild.skipped",
            project_id=artifact.project_id,
            artifact=artifact.artifact_id,
            reason="already_running",
        )
        return

    if artifact.file_type != "audio":
        pending = _other_pending_jobs(db, artifact)
        if pending:
            log_event(
                logger,
                "tz.rebuild.skipped",
                project_id=artifact.project_id,
                artifact=artifact.artifact_id,
                reason="batch_pending",
                pending_jobs=pending,
            )
            return
        with track_project_task(
            db,
            task_type=TASK_TZ_REBUILD,
            project_id=artifact.project_id,
        ):
            rebuild_tz_fast(
                db,
                settings,
                artifact.project_id,
                summary=f"auto after document {artifact.artifact_id}",
            )
        return

    n = settings.tz_rebuild_every_n
    count = db.scalar(
        select(func.count())
        .select_from(Artifact)
        .where(Artifact.project_id == artifact.project_id, Artifact.status == "done")
    )
    if count and count % n == 0:
        with track_project_task(
            db,
            task_type=TASK_TZ_REBUILD,
            project_id=artifact.project_id,
        ):
            rebuild_tz_fast(db, settings, artifact.project_id, summary=f"auto after {count} done artifacts")
    else:
        log_event(
            logger,
            "tz.rebuild.skipped",
            project_id=artifact.project_id,
            artifact=artifact.artifact_id,
            done_artifacts=count or 0,
            every_n=n,
        )
