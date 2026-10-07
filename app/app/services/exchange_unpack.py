"""Раскладка принятой посылки файлов в рабочую копию проекта: распаковка gzip и zip с проверкой, без исполнения содержимого.

Куда (data/projects/<id>/config/):
- `map/` — карта конфигурации: каждый `*.gz` посылки распакован в файл без суффикса; sha256 и размер сверяются с MANIFEST.json посылки, если он есть;
- `deepmaps/<путь zip в посылке без первой папки и .zip>/` — дипмап сущности или сценария кнопки (содержимое zip; открывать index.html),
  новая версия заменяет прежнюю; пути на Windows — с префиксом длинных путей (имена шагов длинные);
- README из посылки — рядом с тем, что описывает: `map/README.md`, `deepmaps/README.md` (из `depmap/`), иначе `README.md`;
- `layout.json` — какие посылки и когда разложены. Посылки накопительные (агент досылает части и описания отдельно): каждая
  раскладка добавляет или заменяет только свои части, остальное остаётся.
Посылка в data/exchange_files остаётся нетронутой: это архив принятых версий, рабочая копия собирается из неё заново.
Запуск: сам после приёма посылки целиком (schedule_unpack, фоновый поток, по одной раскладке за раз; EXCHANGE_AUTO_UNPACK)
или вручную — scripts/unpack_exchange_bundle.py. Ошибка раскладки пишется в журнал обмена (files_unpack, rejected).

Границы: пути внутри zip — только относительные, без «..», диска и обратной косой; предел распакованного объёма и степени сжатия;
место на диске проверяется заранее; запись идёт во временную папку и заменяет прежнюю копию только после полной проверки.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.app.config import Settings
from app.app.services import exchange_files as xf

MAX_RATIO = 200  # распакованное / сжатое для одного файла zip; больше — похоже на zip-бомбу
MAX_UNPACKED = 64 * 1024 ** 3
ZIP_MAX_PATH, ZIP_MAX_PART = 400, 255  # пути внутри zip длиннее, чем у файлов посылки: имена шагов дипмапа доходят до 130 знаков

_unpack_lock = threading.Lock()  # одна раскладка за раз: посылки раскладываются в порядке приёма


def project_config_dir(settings: Settings, project_id: int) -> Path:
    return Path(settings.data_dir) / "projects" / str(project_id) / "config"


def _copy_hash(src, dst: Path, limit: int) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("wb") as out:
        while block := src.read(8 * 1024 * 1024):
            n += len(block)
            if n > limit:
                raise xf.FilesError(f"{dst.name}: распакованный объём больше предела", "unpack_too_large")
            h.update(block)
            out.write(block)
    return h.hexdigest(), n


def _zip_member_path(name: str) -> str | None:
    """Относительный путь члена zip или None для папки; опасный путь — отказ."""
    if name.endswith("/"):
        return None
    if "\\" in name or ":" in name or name.startswith("/"):
        raise xf.FilesError(f"zip: недопустимый путь {name[:80]}", "bad_zip")
    return xf.normalize_path(name, max_path=ZIP_MAX_PATH, max_part=ZIP_MAX_PART)


def _plan(root: Path, files: list) -> tuple[dict[str, dict], int]:
    """Что и сколько распакуется: манифест исходников агента (если есть) и общий объём."""
    manifest: dict[str, dict] = {}
    for f in files:
        if f.path.rsplit("/", 1)[-1] == "MANIFEST.json":
            try:
                data = json.loads((root / f.path).read_text(encoding="utf-8"))
                for item in data.get("files") or []:
                    if isinstance(item, dict) and item.get("path"):
                        manifest[str(item["path"])] = item
            except (OSError, ValueError, AttributeError):
                pass
    total = 0
    for f in files:
        p = root / f.path
        if f.path.endswith(".gz"):
            src = manifest.get(f.path, {}).get("source_bytes")
            total += int(src) if isinstance(src, int) else f.size * 20
        elif f.path.endswith(".zip"):
            with zipfile.ZipFile(p) as z:
                total += sum(i.file_size for i in z.infolist())
        else:
            total += f.size
    return manifest, total


def unpack_bundle(db: Session, settings: Settings, project_id: int, bundle_id: Any) -> dict[str, Any]:
    """Разложить принятую посылку в data/projects/<id>/config. Повтор безопасен: копия собирается заново и заменяет прежнюю."""
    bundle = xf.get_bundle(db, project_id, bundle_id)
    if bundle.status != "complete":
        raise xf.FilesError(f"Посылка {bundle.id} ещё не принята целиком", "not_ready")
    files = xf._files(db, bundle.id)
    src_root = xf.long_path(xf._bundle_dir(settings, project_id, bundle.id) / "files")
    for f in files:  # файлы посылки не должны были измениться с момента приёма
        if xf._hash_file(src_root / f.path) != f.sha256:
            raise xf.FilesError(f"{f.path}: sha256 на диске не совпал с принятым", "hash_mismatch")
    manifest, total = _plan(src_root, files)
    if total > MAX_UNPACKED:
        raise xf.FilesError(f"распакованный объём {total} больше предела", "unpack_too_large")
    dest = project_config_dir(settings, project_id)
    dest.mkdir(parents=True, exist_ok=True)
    dest = xf.long_path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(dest).free - total < settings.exchange_files_reserve_gb * 1024 ** 3:
        raise xf.FilesError(f"мало места на диске для распаковки {total / 1024 ** 3:.1f} ГБ", "disk_full")

    stage = dest / f".stage_b{bundle.id}"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    report: dict[str, Any] = {"map": [], "deepmaps": [], "other": [], "readme": [], "checked_sources": 0}
    try:
        for f in files:
            src = src_root / f.path
            name = f.path.rsplit("/", 1)[-1]
            if name.endswith(".gz"):
                rel = f.path.split("/", 1)[1] if f.path.startswith("map/") else f.path
                out = stage / "map" / rel[:-3]
                want = manifest.get(f.path, {})
                limit = int(want["source_bytes"]) if isinstance(want.get("source_bytes"), int) else f.size * 1000
                with gzip.open(src, "rb") as g:
                    sha, n = _copy_hash(g, out, limit)
                if want.get("source_sha256"):
                    if (sha, n) != (want["source_sha256"], want.get("source_bytes")):
                        raise xf.FilesError(f"{f.path}: после распаковки sha256 или размер не совпали с MANIFEST.json", "hash_mismatch")
                    report["checked_sources"] += 1
                report["map"].append({"path": "map/" + rel[:-3], "bytes": n, "sha256": sha})
            elif name.endswith(".zip"):
                # дипмап кладётся по пути zip внутри посылки без первой папки: depmap/Документ X.zip → «Документ X»,
                # depmap/scenario_<кнопка>/Обработка X.zip → «scenario_<кнопка>/Обработка X» (одинаковые имена zip в разных папках не затирают друг друга)
                entity = f.path.split("/", 1)[1][:-4] if "/" in f.path else name[:-4]
                leaf = entity.rsplit("/", 1)[-1]
                with zipfile.ZipFile(src) as z:
                    for info in z.infolist():
                        rel = _zip_member_path(info.filename)
                        if rel is None:
                            continue
                        if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
                            raise xf.FilesError(f"{name}: {rel} сжат подозрительно сильно", "bad_zip")
                        parts = rel.split("/")
                        if len(parts) > 1 and parts[0] == leaf:  # zip уже содержит папку сущности
                            parts = parts[1:]
                        with z.open(info) as m:  # zipfile сам сверяет CRC при чтении до конца
                            _copy_hash(m, stage / "deepmaps" / Path(*entity.split("/")) / Path(*parts), info.file_size)
                report["deepmaps"].append(entity)
            elif name.lower() == "readme.md":
                # описание кладётся к тому, что описывает: map/README.md → map/, depmap/README.md → deepmaps/, иначе — в корень
                top = f.path.split("/", 1)[0] if "/" in f.path else ""
                rel = {"map": "map/README.md", "depmap": "deepmaps/README.md"}.get(top, "README.md")
                out = stage / "readme" / Path(*rel.split("/"))
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, out)
                report["readme"].append(rel)
                report["other"].append(f.path)
            else:
                out = stage / ("map" if f.path.startswith("map/") else "other") / f.path.split("/", 1)[-1]
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, out)
                report["other"].append(f.path)

        # замена рабочей копии: карта — целиком; дипмапы — по сущностям; README — последний
        if (stage / "map").exists():
            shutil.rmtree(dest / "map", ignore_errors=True)
            (stage / "map").replace(dest / "map")
        for entity in report["deepmaps"]:
            target = dest / "deepmaps" / Path(*entity.split("/"))
            shutil.rmtree(target, ignore_errors=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            (stage / "deepmaps" / Path(*entity.split("/"))).replace(target)
        for rel in report["readme"]:  # после замены map/: описание карты не должно пропасть вместе со старой папкой
            target = dest / Path(*rel.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            (stage / "readme" / Path(*rel.split("/"))).replace(target)
        if (stage / "other").exists():
            shutil.rmtree(dest / "other", ignore_errors=True)
            (stage / "other").replace(dest / "other")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    layout_path = dest / "layout.json"
    try:
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        layout = {}
    entry = {"bundle_id": bundle.id, "name": bundle.name, "kind": bundle.kind, "unpacked_at": datetime.now(timezone.utc).isoformat(),
             "map_files": len(report["map"]), "deepmaps": report["deepmaps"], "checked_sources": report["checked_sources"]}
    if report["map"]:
        layout["map"] = {**entry, "files": report["map"]}
    for entity in report["deepmaps"]:
        layout.setdefault("deepmaps", {})[entity] = {"bundle_id": bundle.id, "name": bundle.name, "unpacked_at": entry["unpacked_at"]}
    layout.setdefault("history", []).append(entry)
    layout_path.write_text(json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8")
    xf._log(db, project_id, "owner", "files_unpack", "accepted", bundle=bundle, size=total,
            response={"name": bundle.name, "map_files": len(report["map"]), "deepmaps": len(report["deepmaps"])})
    return {**entry, "dest": str(dest)}


def _unpack_logged(factory, settings: Settings, project_id: int, bundle_id: int) -> None:
    with _unpack_lock:
        db = factory()
        try:
            unpack_bundle(db, settings, project_id, bundle_id)
        except Exception as exc:  # фоновый поток: ошибка — в журнал обмена, а не в никуда
            db.rollback()
            try:
                bundle = xf.get_bundle(db, project_id, bundle_id)
            except xf.FilesError:
                bundle = None
            code = getattr(exc, "code", type(exc).__name__)
            xf._log(db, project_id, "owner", "files_unpack", "rejected", bundle=bundle, reasons=[f"{code}: {str(exc)[:500]}"])
        finally:
            db.close()


def schedule_unpack(db: Session, settings: Settings, project_id: int, bundle_id: int) -> threading.Thread:
    """Разложить посылку фоном (распаковка карты — десятки секунд, приём последней части ждать не должен)."""
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False)
    t = threading.Thread(target=_unpack_logged, args=(factory, settings, project_id, bundle_id), daemon=True, name=f"unpack-b{bundle_id}")
    t.start()
    return t
