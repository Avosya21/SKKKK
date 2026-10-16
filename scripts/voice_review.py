"""Разбор базы голосов и людей проектов на слух: локальная страница, решения владельца, применение.

  venv\\Scripts\\python.exe scripts\\voice_review.py build            # собрать страницу и фрагменты голосов
  venv\\Scripts\\python.exe scripts\\voice_review.py serve            # открыть http://127.0.0.1:8770 (только этот компьютер)
  venv\\Scripts\\python.exe scripts\\voice_review.py apply            # показать, что изменится по решениям (база не меняется)
  venv\\Scripts\\python.exe scripts\\voice_review.py apply --yes      # применить; перед этим копия БД в data/backups/

Страница: карточка на каждый голос (эталон и реплики из разных созвонов с текстом, проекты, люди проектов под этим голосом,
похожие голоса), вкладка возможных дублей «A против B», группы по компаниям — исполнители, затем заказчики. Решения
сохраняются сами в ``decisions.json`` рядом со страницей. Всё лежит в ``data/people/voice_review/`` — вне git: там имена и
голоса людей. Сервер слушает только 127.0.0.1.

Решения по голосу: «всё верно», «переименовать», «это тот же человек, что …» (слить), «в голосе смешаны разные люди»,
«удалить голос» (привязки в созвонах снимаются — голос снова «новый»), «не уверен»; компания и сторона — людям проекта под
этим голосом (компания — из списка компаний портала). Решение по паре: один человек (какой оставить) / разные / не уверен. Голос без владельца получает предложение «чей голос»
(ЛГ4 PEOPLE-FLOW-PLAN): учётка сотрудника с согласием в карточке или человек проекта; привязка — только по «Да».
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "people" / "voice_review"  # data/people/ — вне git
PAGE = Path(__file__).with_suffix(".html")
DECISIONS = OUT / "decisions.json"
PORT = 8770
CALLS_PER_VOICE = 4
SAMPLE_MAX_SEC = 15.0
SAMPLE_MIN_SEC = 3.0
NEIGHBOURS = 4
NEIGHBOUR_MIN = 0.55


# ---------------------------------------------------------------- сборка

def _clip(wav: Path, start: float, end: float, out: Path) -> bool:
    if out.is_file():
        return True
    if not wav.is_file() or end <= start:
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(wav),
           "-ac", "1", "-b:a", "48k", str(out)]
    return subprocess.run(cmd, capture_output=True).returncode == 0 and out.is_file()


def _speaker_sample(turns: list, name: str, near: float | None) -> tuple[float, float, str] | None:
    """Длинная реплика этого голоса в созвоне (до SAMPLE_MAX_SEC), ближе к эталонному отрезку; с текстом."""
    from app.app.services.speaker_profiles import fold_speaker_display_name as fold

    key = fold(name)
    own = [t for t in turns if t.is_speech and t.start_sec is not None and t.end_sec is not None
           and fold(t.speaker or "") == key and (t.end_sec - t.start_sec) >= SAMPLE_MIN_SEC]
    if not own:
        return None
    best = max(own, key=lambda t: (min(t.end_sec - t.start_sec, SAMPLE_MAX_SEC),
                                   -abs((near or t.start_sec) - t.start_sec) / 3600))
    end = min(best.end_sec, best.start_sec + SAMPLE_MAX_SEC)
    return best.start_sec, end, (best.text or "").strip()[:400]


def owner_suggestion(prof, linked_people: list, consent_of) -> dict | None:
    """ЛГ4: чей голос без владельца. Один сотрудник с согласием в карточке среди людей под голосом — его учётка (Р5);
    сотрудников нет, а человек проекта один — он (голос внешнего человека, Д5). Иначе — без предложения."""
    if prof.user_id or prof.person_id:
        return None
    staff = {p.user_id: p for p in linked_people if p.user_id}
    if len(staff) == 1:
        uid, person = next(iter(staff.items()))
        if consent_of(uid):
            return {"kind": "account", "user_id": uid, "label": f"учётка сотрудника «{person.display_name}»"}
        return None
    if not staff and len(linked_people) == 1:
        person = linked_people[0]
        return {"kind": "person", "person_id": person.id, "project": person.project_id,
                "label": f"человек проекта {person.project_id} «{person.display_name}»"}
    return None


def build(skip_projects: set[int] | None = None) -> Path:
    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.company import Company
    from app.app.models.person import Person, PersonAlias, ProjectParty
    from app.app.models.project import Project
    from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile
    from app.app.models.user import User
    from app.app.services import people, voice_link
    from app.app.services.transcript_turns import load_turns
    from asr.speaker_registry import cosine, unpack_embedding
    from scripts.voice_duplicates import candidate_pairs

    settings = get_settings()
    db = SessionLocal()
    OUT.mkdir(parents=True, exist_ok=True)
    clips = OUT / "clips"
    try:
        profiles = db.scalars(select(SpeakerProfile).order_by(SpeakerProfile.id)).all()
        projects = {p.id: p.name for p in db.scalars(select(Project))}
        arts = {a.artifact_id: a for a in db.scalars(select(Artifact).where(Artifact.file_type == "audio"))}
        users = {u.id: u.name for u in db.scalars(select(User))}
        links: dict[int, list] = defaultdict(list)
        for link in db.scalars(select(ArtifactSpeakerLink).where(ArtifactSpeakerLink.status == "confirmed",
                                                                ArtifactSpeakerLink.profile_id.isnot(None))):
            links[link.profile_id].append(link)
        persons = db.scalars(select(Person).where(Person.merged_into.is_(None), Person.is_active.is_(True))).all()
        by_alias: dict[tuple[int, str], Person] = {}
        for a in db.scalars(select(PersonAlias)):
            p = db.get(Person, a.person_id)
            if p is not None:
                by_alias[(a.project_id, a.alias_key)] = people.canonical(db, p)
        # выбор компании на странице: справочник компаний портала и стороны проектов («Люди и стороны»)
        portal: dict[str, dict] = {}
        for c in db.scalars(select(Company).where(Company.active.is_(True))):
            portal.setdefault(c.name.casefold(), {"name": c.name, "side": c.side or ""})
        companies = {c.name: c.side for c in db.scalars(select(Company))}
        for party in db.scalars(select(ProjectParty)):
            companies.setdefault(party.company_name, party.side)
            row = portal.setdefault(party.company_name.casefold(), {"name": party.company_name, "side": party.side or ""})
            if row["side"] != (party.side or row["side"]):
                row["side"] = ""  # в разных проектах на разных сторонах — сторону не подставляем
        for p in persons:
            if p.company_name:
                companies.setdefault(p.company_name, "")

        vecs = {p.id: unpack_embedding(p.embedding) for p in profiles}
        turns_cache: dict[str, list] = {}
        jobs = []
        voices = []
        for prof in profiles:
            calls = sorted(links.get(prof.id, []), key=lambda l: l.artifact_id, reverse=True)
            # люди проектов под этим голосом: свой слепок, учётка, имя голоса в реестре проектов, где он звучит
            linked: dict[int, Person] = {}
            for p in persons:
                if (prof.person_id and p.id == prof.person_id) or (prof.user_id and p.user_id == prof.user_id):
                    linked[p.id] = p
            for pid in {l.project_id for l in calls}:
                hit = by_alias.get((pid, people.alias_key(prof.display_name)))
                if hit is not None:
                    linked[hit.id] = hit
            samples = []
            if prof.source_artifact_id in arts and prof.source_start_sec is not None:
                art = arts[prof.source_artifact_id]
                out = clips / f"v{prof.id}_ref.mp3"
                jobs.append((Path(art.file_path), float(prof.source_start_sec), float(prof.source_end_sec or prof.source_start_sec + 8), out))
                samples.append({"kind": "эталон", "src": f"clips/{out.name}", "project": art.project_id,
                                "call": art.original_filename, "at": prof.source_start_sec, "text": ""})
            seen_calls = set()
            for link in calls:
                if link.artifact_id in seen_calls or link.artifact_id not in arts or len(seen_calls) >= CALLS_PER_VOICE:
                    continue
                seen_calls.add(link.artifact_id)
                art = arts[link.artifact_id]
                if art.artifact_id not in turns_cache:
                    try:
                        turns_cache[art.artifact_id] = load_turns(settings, art)
                    except Exception:  # noqa: BLE001 — нет стенограммы: только отрезок привязки
                        turns_cache[art.artifact_id] = []
                sample = _speaker_sample(turns_cache[art.artifact_id], prof.display_name, link.clip_start_sec)
                if sample is None and link.clip_start_sec is not None:
                    sample = (float(link.clip_start_sec), float(link.clip_end_sec or link.clip_start_sec + 6), "")
                if sample is None:
                    continue
                out = clips / f"v{prof.id}_{art.id}.mp3"
                jobs.append((Path(art.file_path), sample[0], sample[1], out))
                samples.append({"kind": "созвон", "src": f"clips/{out.name}", "project": art.project_id,
                                "call": art.original_filename, "at": round(sample[0]), "text": sample[2],
                                "label": link.diar_label})
            voice_projects = {l.project_id for l in calls} | ({prof.project_id} if prof.project_id else set())
            suggest = None if skip_projects and voice_projects & skip_projects else                 owner_suggestion(prof, list(linked.values()), lambda uid: voice_link.has_voice_consent(db, uid))
            near = sorted(((cosine(vecs[prof.id], vecs[o.id]), o.id) for o in profiles
                           if o.id != prof.id and len(vecs[o.id]) == len(vecs[prof.id])), reverse=True)
            voices.append({
                "id": prof.id, "name": prof.display_name, "scope": prof.scope,
                "account": users.get(prof.user_id) if prof.user_id else None,
                "calls": len({l.artifact_id for l in calls}),
                "projects": sorted({l.project_id for l in calls} | ({prof.project_id} if prof.project_id else set())),
                "people": [{"id": p.id, "project": p.project_id, "name": p.display_name, "company": p.company_name or "",
                            "side": p.side, "job": p.job_title or "", "account": p.user_id is not None} for p in linked.values()],
                "samples": samples,
                "near": [{"id": oid, "pct": int(round(s * 100))} for s, oid in near[:NEIGHBOURS] if s >= NEIGHBOUR_MIN],
                "owner": ("учётка" if prof.user_id else "человек проекта" if prof.person_id else None),
                "owner_suggest": suggest,
            })
        with ThreadPoolExecutor(max_workers=6) as pool:
            ok = list(pool.map(lambda j: _clip(*j), jobs))
        missing = {str(j[3].name) for j, good in zip(jobs, ok) if not good}
        for v in voices:
            v["samples"] = [s for s in v["samples"] if Path(s["src"]).name not in missing]
        pairs = [{"a": p["a"].id, "b": p["b"].id, "pct": int(round(p["cos"] * 100)), "names": p["rel"]}
                 for p in candidate_pairs(db)]
        data = {"built": datetime.now().strftime("%d.%m.%Y %H:%M"), "voices": voices, "pairs": pairs,
                "projects": {str(k): v for k, v in projects.items()},
                "companies": [{"name": n, "side": s or ""} for n, s in sorted(companies.items(), key=lambda x: x[0].casefold())],
                "portal_companies": sorted(portal.values(), key=lambda c: c["name"].casefold())}
        (OUT / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        shutil.copy2(PAGE, OUT / "index.html")
        print(f"голосов: {len(voices)}, фрагментов: {len(jobs) - len(missing)} (не вырезано {len(missing)}), "
              f"пар на проверку: {len(pairs)} → {OUT}")
        return OUT
    finally:
        db.close()


# ---------------------------------------------------------------- страница

class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(OUT), **k)

    def log_message(self, *_a):  # тихо
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/decisions"):
            body = DECISIONS.read_bytes() if DECISIONS.is_file() else b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802
        if not self.path.startswith("/decisions"):
            self.send_error(404)
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            json.loads(raw.decode("utf-8"))
        except ValueError:
            self.send_error(400)
            return
        tmp = DECISIONS.with_suffix(".tmp")
        tmp.write_bytes(raw)
        tmp.replace(DECISIONS)
        self.send_response(204)
        self.end_headers()


def serve() -> None:
    if not (OUT / "data.json").is_file():
        build()
    shutil.copy2(PAGE, OUT / "index.html")  # свежая разметка без пересборки фрагментов
    server = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    print(f"Разбор голосов: http://127.0.0.1:{PORT}  (Ctrl+C — остановить; решения пишутся в {DECISIONS})")
    server.serve_forever()


# ---------------------------------------------------------------- применение

def plan(decisions: dict, data: dict) -> list[tuple[str, dict]]:
    """Решения → шаги: слить, переименовать, компания/сторона людям, удалить голос. Пары «один человек» — тоже слияния."""
    steps: list[tuple[str, dict]] = []
    names = {v["id"]: v["name"] for v in data["voices"]}
    suggestions = {v["id"]: v.get("owner_suggest") for v in data["voices"]}
    merges: dict[int, int] = {}
    for key, verdict in (decisions.get("pairs") or {}).items():
        a, b = (int(x) for x in key.split(":"))
        if verdict == "keep_a":
            merges[b] = a
        elif verdict == "keep_b":
            merges[a] = b
    for vid, d in (decisions.get("voices") or {}).items():
        if d.get("verdict") == "merge" and d.get("merge_into"):
            merges[int(vid)] = int(d["merge_into"])

    def root(v: int) -> int:
        seen = set()
        while v in merges and v not in seen:
            seen.add(v)
            v = merges[v]
        return v

    for dup in sorted(merges):
        keep = root(dup)
        if keep != dup:
            steps.append(("merge", {"keep": keep, "dup": dup, "text": f"слить «{names.get(dup)}» ({dup}) → «{names.get(keep)}» ({keep})"}))
    for vid, d in (decisions.get("voices") or {}).items():
        vid = int(vid)
        target = root(vid)
        name = (d.get("name") or "").strip()
        if d.get("verdict") == "rename" and name and name != names.get(vid):
            steps.append(("rename", {"id": target, "old": names.get(vid), "name": name,
                                     "text": f"переименовать «{names.get(vid)}» ({vid}) → «{name}»"}))
        if d.get("verdict") == "delete":
            if vid in merges:
                steps.append(("note", {"id": vid, "text": f"«{names.get(vid)}» ({vid}): отмечен «удалить», но и сливается — "
                                                         "оставлено слияние, проверьте"}))
            else:
                steps.append(("delete", {"id": vid, "text": f"удалить голос «{names.get(vid)}» ({vid}), привязки в созвонах снять"}))
        if (d.get("company") or "").strip() or d.get("side"):
            steps.append(("person", {"id": target, "company": (d.get("company") or "").strip(), "side": d.get("side") or "",
                                     "text": f"людям проектов под «{name or names.get(vid)}»: компания «{d.get('company') or '—'}», "
                                             f"сторона {d.get('side') or '—'}"}))
        if d.get("verdict") == "mixed":
            steps.append(("note", {"id": vid, "text": f"«{names.get(vid)}» ({vid}): смешаны разные люди — разбирать вручную"}))
        offer = suggestions.get(vid)
        if d.get("owner") == "accept" and offer and vid not in merges and d.get("verdict") != "delete":
            steps.append(("owner", {"id": vid, **offer, "text": f"голос «{names.get(vid)}» ({vid}) → {offer['label']}"}))
    return steps


def apply(yes: bool) -> int:
    if not DECISIONS.is_file():
        print("решений пока нет:", DECISIONS)
        return 1
    decisions = json.loads(DECISIONS.read_text(encoding="utf-8"))
    data = json.loads((OUT / "data.json").read_text(encoding="utf-8"))
    steps = plan(decisions, data)
    for kind, step in steps:
        print(f"[{kind}] {step['text']}")
    print(f"шагов: {len(steps)}")
    if not yes or not steps:
        print("база не менялась; применить — с --yes")
        return 0
    _apply_steps(steps, data)
    return 0


def _apply_steps(steps: list[tuple[str, dict]], data: dict) -> None:
    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile
    from app.app.services import people, voice_link
    from app.app.services.entities import rebind_call_entities
    from app.app.services.speaker_profiles import _absorb_profile, _refresh_match_views_after_cleanup

    settings = get_settings()
    db_path = settings.data_dir / "app.db"
    backup = db_path.parent / "backups" / f"app.db.bak-voice-review-{datetime.now():%Y%m%d-%H%M%S}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, backup)
    print("копия БД:", backup)
    people_of = {v["id"]: v["people"] for v in data["voices"]}
    touched: set[str] = set()
    db = SessionLocal()
    try:
        for kind, s in steps:
            if kind == "merge":
                keep, dup = db.get(SpeakerProfile, s["keep"]), db.get(SpeakerProfile, s["dup"])
                if keep is None or dup is None:
                    print("пропуск:", s["text"])
                    continue
                touched |= {l.artifact_id for l in db.scalars(select(ArtifactSpeakerLink).where(ArtifactSpeakerLink.profile_id == dup.id))}
                if keep.user_id is None and dup.user_id is not None:
                    keep.user_id = dup.user_id
                _absorb_profile(db, keep, dup)
                people_of.setdefault(keep.id, []).extend(people_of.get(dup.id, []))
            elif kind == "rename":
                prof = db.get(SpeakerProfile, s["id"])
                if prof is None:
                    continue
                prof.display_name = s["name"]
                touched |= {l.artifact_id for l in db.scalars(select(ArtifactSpeakerLink).where(ArtifactSpeakerLink.profile_id == prof.id))}
                for p in people_of.get(s["id"], []):
                    if people.alias_key(p["name"]) != people.alias_key(s["name"]):
                        try:
                            people.update_person(db, p["project"], p["id"], display_name=s["name"])
                        except people.PeopleError as exc:
                            print(f"  человек {p['id']} (проект {p['project']}): {exc}")
            elif kind == "delete":
                prof = db.get(SpeakerProfile, s["id"])
                if prof is None:
                    continue
                for link in db.scalars(select(ArtifactSpeakerLink).where(ArtifactSpeakerLink.profile_id == prof.id)):
                    touched.add(link.artifact_id)
                    link.profile_id, link.status = None, "pending"
                for link in db.scalars(select(ArtifactSpeakerLink).where(ArtifactSpeakerLink.suggested_profile_id == prof.id)):
                    link.suggested_profile_id = None
                db.delete(prof)
            elif kind == "owner":
                prof = db.get(SpeakerProfile, s["id"])
                if prof is None or prof.user_id or prof.person_id:
                    print("пропуск:", s["text"])
                    continue
                if s["kind"] == "account":
                    voice_link.link_profile(db, prof.id, s["user_id"])  # согласие в карточке проверяется здесь
                else:
                    prof.person_id = s["person_id"]
                touched |= {l.artifact_id for l in db.scalars(select(ArtifactSpeakerLink).where(ArtifactSpeakerLink.profile_id == prof.id))}
            elif kind == "person":
                for p in people_of.get(s["id"], []):
                    try:
                        if s["company"]:
                            people.update_person(db, p["project"], p["id"], company_name=s["company"])
                            if s["side"] in ("executor", "customer") and people.party_side(db, p["project"], s["company"]) is None:
                                people.set_party(db, p["project"], s["company"], s["side"])
                        if s["side"] in ("executor", "customer"):
                            people.set_side(db, p["project"], p["id"], s["side"], source="human", force=True)
                    except people.PeopleError as exc:
                        print(f"  человек {p['id']} (проект {p['project']}): {exc}")
            db.commit()
            print("готово:", s["text"])
        print("обновлено страниц созвонов:", _refresh_match_views_after_cleanup(db))
        for aid in sorted(touched):
            art = db.scalar(select(Artifact).where(Artifact.artifact_id == aid))
            if art is not None:
                rebind_call_entities(db, settings, art)
                db.commit()
        print("перепривязаны пункты созвонов:", len(touched))
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=("build", "serve", "apply"))
    ap.add_argument("--yes", action="store_true", help="apply: применить (иначе только план)")
    ap.add_argument("--skip-project", type=int, action="append", default=[],
                    help="build: не предлагать владельца голосам, звучащим в этом проекте (можно несколько раз)")
    args = ap.parse_args()
    if args.cmd == "build":
        build(set(args.skip_project))
    elif args.cmd == "serve":
        serve()
    else:
        return apply(args.yes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
