"""Посылки файлов от агента: право ключа, манифест, приём частями с докачкой и sha256, только добавление, выдача по HTTP и MCP."""
from __future__ import annotations

import base64
import hashlib
import json

import pytest

from app.app.config import get_settings
from app.app.mcp.project_protocol import call_tool
from app.app.services import exchange_files as xf
from app.app.services.project_mcp_tokens import create_project_mcp_token, list_project_mcp_tokens
from app.app.tests.test_contract import _seed


@pytest.fixture
def env(client, manager_token, tmp_data, monkeypatch):
    monkeypatch.setenv("EXCHANGE_FILES_RESERVE_GB", "0")
    monkeypatch.setenv("EXCHANGE_AUTO_UNPACK", "0")  # автораскладку проверяет отдельный тест
    get_settings.cache_clear()
    db, pid, art, _ = _seed(client, manager_token, tmp_data)
    yield client, manager_token, tmp_data, db, pid, art
    get_settings.cache_clear()


def _key(db, pid, art, files_access):
    row, plain = create_project_mcp_token(db, project_id=pid, label="t", user_id=art.user_id, files_access=files_access)
    return row, {"Authorization": f"Bearer {plain}"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _call(db, pid, token, tool, **args):
    res = call_tool(db, get_settings(), pid, tool, args, token=token)
    return res["isError"], json.loads(res["content"][0]["text"])


def test_paths_and_manifest_are_checked():
    for bad in ("/abs", "C:/x", "a\\b", "../x", "a/../b", "a//b", "con.txt", "a/ b", "a."):
        with pytest.raises(xf.FilesError):
            xf.normalize_path(bad)
    assert xf.normalize_path("map/config_nav.sqlite") == "map/config_nav.sqlite"
    sha = "0" * 64
    with pytest.raises(xf.FilesError, match="повторяется"):
        xf._normalize_manifest([{"path": "A.db", "size": 1, "sha256": sha}, {"path": "a.db", "size": 1, "sha256": sha}])
    with pytest.raises(xf.FilesError, match="файл и папка"):
        xf._normalize_manifest([{"path": "a", "size": 1, "sha256": sha}, {"path": "a/b", "size": 1, "sha256": sha}])


def test_default_key_reads_but_cannot_send_blocked_key_sees_nothing(env):
    client, manager_token, _, db, pid, art = env
    row, h = _key(db, pid, art, None)
    assert list_project_mcp_tokens(db, project_id=pid)[0]["files_access"] == "read"  # файлы проекта видит любой ключ
    r = client.post(f"/mcp/projects/{pid}/files/bundles", headers=h, json={"name": "m", "files": [{"path": "a", "size": 1, "sha256": "0" * 64}]})
    assert r.status_code == 403 and r.json()["detail"] == "files_not_allowed"
    err, body = _call(db, pid, row, "files_list_bundles")
    assert not err and body["items"] == []
    mh = {"Authorization": f"Bearer {manager_token}"}
    # «none» — прежнее имя закрытого доступа
    assert client.post(f"/projects/{pid}/mcp/tokens/{row.id}/files", headers=mh, json={"files_access": "none"}).json()["files_access"] == "blocked"
    db.refresh(row)
    for tool in ("files_list_bundles", "files_catalog"):
        err, body = _call(db, pid, row, tool)
        assert err and body["detail"] == "files_not_allowed"
    assert client.get(f"/mcp/projects/{pid}/files/catalog", headers=h).status_code == 403


def test_http_upload_resume_hash_and_download(env):
    client, _, tmp, db, pid, art = env
    _, h = _key(db, pid, art, "write")
    big, small = bytes(range(256)) * 40, b"stamp"
    manifest = [{"path": "map/config_nav.sqlite", "size": len(big), "sha256": _sha(big)},
                {"path": "map/stamps/01.done", "size": len(small), "sha256": _sha(small)},
                {"path": "map/empty.log", "size": 0, "sha256": _sha(b"")}]
    r = client.post(f"/mcp/projects/{pid}/files/bundles", headers=h, json={"name": "config_map_1", "kind": "config_map", "files": manifest})
    assert r.status_code == 200, r.text
    b = r.json()
    bid = b["bundle_id"]
    assert b["status"] == "open" and {f["path"] for f in b["files"]} == {"map/config_nav.sqlite", "map/stamps/01.done"}  # пустой принят сразу
    url = f"/mcp/projects/{pid}/files/bundles/{bid}/data/map/config_nav.sqlite"
    assert client.put(url + "?offset=0", headers=h, content=big[:4000]).json()["received"] == 4000
    # обрыв: повтор с неверной позиции получает, откуда продолжать
    r = client.put(url + "?offset=0", headers=h, content=big[:4000])
    assert r.status_code == 409 and r.json()["expected_offset"] == 4000
    # испорченная в пути часть не пишется
    r = client.put(url + "?offset=4000", headers={**h, "X-Chunk-Sha256": "f" * 64}, content=big[4000:])
    assert r.status_code == 400 and r.json()["detail"] == "chunk_hash_mismatch"
    missing = client.get(f"/mcp/projects/{pid}/files/bundles/{bid}?files=missing", headers=h).json()["files"]
    assert {f["path"]: f["received"] for f in missing}["map/config_nav.sqlite"] == 4000
    r = client.put(url + "?offset=4000", headers={**h, "X-Chunk-Sha256": _sha(big[4000:])}, content=big[4000:])
    assert r.json()["status"] == "complete" and r.json()["bundle_status"] == "open"
    # файл вне манифеста и за пределом размера
    assert client.put(f"/mcp/projects/{pid}/files/bundles/{bid}/data/other.bin?offset=0", headers=h, content=b"x").status_code == 404
    r = client.put(f"/mcp/projects/{pid}/files/bundles/{bid}/data/map/stamps/01.done?offset=0", headers=h, content=small)
    assert r.json()["bundle_status"] == "complete"
    # принятое не перезаписывается
    assert client.put(url + "?offset=0", headers=h, content=b"y").json()["detail"] == "bundle_closed"
    # скачивание целиком и с Range
    d = client.get(url, headers=h)
    assert d.status_code == 200 and d.content == big and d.headers["x-sha256"] == _sha(big)
    part = client.get(url, headers={**h, "Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == big[100:200]
    files_root = tmp / "exchange_files" / f"p{pid}" / f"b{bid}" / "files"
    assert (files_root / "map" / "config_nav.sqlite").read_bytes() == big and (files_root / "map" / "empty.log").exists()


def test_wrong_whole_file_hash_resets_file(env):
    client, _, _, db, pid, art = env
    _, h = _key(db, pid, art, "write")
    data = b"abcdef"
    bid = client.post(f"/mcp/projects/{pid}/files/bundles", headers=h,
                      json={"name": "bad", "files": [{"path": "x.bin", "size": 6, "sha256": _sha(b"zzzzzz")}]}).json()["bundle_id"]
    r = client.put(f"/mcp/projects/{pid}/files/bundles/{bid}/data/x.bin?offset=0", headers=h, content=data).json()
    assert r["status"] == "hash_mismatch" and r["received"] == 0


def test_reopen_same_manifest_resumes_other_manifest_refused(env):
    client, _, _, db, pid, art = env
    row, h = _key(db, pid, art, "write")
    m = [{"path": "a.bin", "size": 3, "sha256": _sha(b"abc")}]
    first = client.post(f"/mcp/projects/{pid}/files/bundles", headers=h, json={"name": "v1", "files": m}).json()
    again = client.post(f"/mcp/projects/{pid}/files/bundles", headers=h, json={"name": "v1", "files": m}).json()
    assert again["resumed"] is True and again["bundle_id"] == first["bundle_id"]
    m2 = [{"path": "a.bin", "size": 4, "sha256": _sha(b"abcd")}]
    r = client.post(f"/mcp/projects/{pid}/files/bundles", headers=h, json={"name": "v1", "files": m2})
    assert r.status_code == 409 and r.json()["detail"] == "name_taken"
    journal = client.get(f"/projects/{pid}/agent-exchange", headers={"Authorization": f"Bearer {env[1]}"}).json()["items"]
    assert any(i.get("kind") == "files_open" for i in journal)


def test_disk_reserve_blocks_open(env, monkeypatch):
    client, _, _, db, pid, art = env
    _, h = _key(db, pid, art, "write")
    monkeypatch.setattr(xf, "_free_bytes", lambda settings: 10 * 1024 ** 3)
    monkeypatch.setenv("EXCHANGE_FILES_RESERVE_GB", "20")
    get_settings.cache_clear()
    r = client.post(f"/mcp/projects/{pid}/files/bundles", headers=h, json={"name": "big", "files": [{"path": "a", "size": 1, "sha256": "0" * 64}]})
    assert r.status_code == 507 and r.json()["detail"] == "disk_full"


def test_mcp_tools_round_trip_and_read_only_key(env):
    client, manager_token, _, db, pid, art = env
    writer, _ = _key(db, pid, art, "write")
    reader, hr = _key(db, pid, art, "read")
    data = b"0123456789" * 50
    err, b = _call(db, pid, writer, "files_open_bundle", name="deepmap_order", kind="entity_deepmap",
                   files=[{"path": "order.zip", "size": len(data), "sha256": _sha(data)}])
    assert not err, b
    bid = b["bundle_id"]
    err, body = _call(db, pid, reader, "files_list_bundles")
    assert not err and body["items"] == []  # читателю незавершённая посылка не видна
    err, body = _call(db, pid, reader, "files_put_chunk", bundle_id=bid, path="order.zip", offset=0, data_base64="AA==")
    assert err and body["detail"] == "files_not_allowed"
    for off in (0, 300):
        err, body = _call(db, pid, writer, "files_put_chunk", bundle_id=bid, path="order.zip", offset=off,
                          data_base64=base64.b64encode(data[off:off + 300]).decode())
        assert not err, body
    assert body["bundle_status"] == "complete"
    err, body = _call(db, pid, reader, "files_get_chunk", bundle_id=bid, path="order.zip", offset=100, length=50)
    assert not err and base64.b64decode(body["data_base64"]) == data[100:150] and body["sha256"] == _sha(data)
    assert client.get(f"/mcp/projects/{pid}/files/bundles/{bid}/data/order.zip", headers=hr).content == data
    # право на файлы меняет владелец; удаляет посылку тоже он
    mh = {"Authorization": f"Bearer {manager_token}"}
    assert client.post(f"/projects/{pid}/mcp/tokens/{reader.id}/files", headers=mh, json={"files_access": "blocked"}).json()["files_access"] == "blocked"
    assert client.post(f"/projects/{pid}/mcp/tokens/{reader.id}/files", headers=mh, json={"files_access": "admin"}).status_code == 422
    assert client.get(f"/projects/{pid}/exchange-files", headers=mh).json()["items"][0]["status"] == "complete"
    assert client.delete(f"/projects/{pid}/exchange-files/{bid}", headers=mh).json()["status"] == "deleted"
    err, body = _call(db, pid, writer, "files_bundle_status", bundle_id=bid)
    assert err and body["detail"] == "not_found"


def _upload(client, h, pid, name, blobs: dict[str, bytes], kind="other"):
    files = [{"path": p, "size": len(b), "sha256": _sha(b)} for p, b in blobs.items()]
    bid = client.post(f"/mcp/projects/{pid}/files/bundles", headers=h, json={"name": name, "kind": kind, "files": files}).json()["bundle_id"]
    for p, b in blobs.items():
        if b:
            assert client.put(f"/mcp/projects/{pid}/files/bundles/{bid}/data/{p}?offset=0", headers=h, content=b).json()["status"] == "complete"
    return bid


def _zip(members: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in members.items():
            z.writestr(n, b)
    return buf.getvalue()


def test_unpack_lays_out_map_and_deepmaps(env):
    import gzip

    from app.app.services.exchange_unpack import project_config_dir, unpack_bundle

    client, _, _, db, pid, art = env
    _, h = _key(db, pid, art, "write")
    nav = b"SQLite format 3\x00" + b"x" * 5000
    manifest = {"files": [{"path": "map/config_nav.sqlite.gz", "source_bytes": len(nav), "source_sha256": _sha(nav)}]}
    b1 = _upload(client, h, pid, "map1", {"map/config_nav.sqlite.gz": gzip.compress(nav, 1), "map/stamps/xml.ok.gz": gzip.compress(b"ok"),
                                          "map/MANIFEST.json": json.dumps(manifest).encode()}, "config_map")
    b2 = _upload(client, h, pid, "dm1", {"depmap/README.md": "# Инструкция".encode(),
                                         "depmap/Документ Заказ.zip": _zip({"Документ Заказ/index.html": b"<html>", "Документ Заказ/data/g.json": b"{}"})},
                 "entity_deepmap")
    settings = get_settings()
    assert unpack_bundle(db, settings, pid, b1)["checked_sources"] == 1
    out = unpack_bundle(db, settings, pid, b2)
    root = project_config_dir(settings, pid)
    assert (root / "map" / "config_nav.sqlite").read_bytes() == nav and (root / "map" / "stamps" / "xml.ok").read_bytes() == b"ok"
    assert (root / "map" / "MANIFEST.json").exists() and (root / "deepmaps" / "README.md").read_text(encoding="utf-8") == "# Инструкция"
    # описание карты досылается отдельной посылкой и ложится в map/, не затирая описание дипмапов и саму карту
    b_readme = _upload(client, h, pid, "map_readme", {"map/README.md": "# О карте".encode()}, "config_map")
    unpack_bundle(db, settings, pid, b_readme)
    assert (root / "map" / "README.md").read_text(encoding="utf-8") == "# О карте" and (root / "map" / "config_nav.sqlite").read_bytes() == nav
    assert (root / "deepmaps" / "README.md").read_text(encoding="utf-8") == "# Инструкция" and not (root / "README.md").exists()
    assert (root / "deepmaps" / "Документ Заказ" / "index.html").read_bytes() == b"<html>" and out["deepmaps"] == ["Документ Заказ"]
    layout = json.loads((root / "layout.json").read_text(encoding="utf-8"))
    assert layout["map"]["bundle_id"] == b1 and layout["deepmaps"]["Документ Заказ"]["bundle_id"] == b2
    # повтор безопасен; zip с путём наружу отклоняется, рабочая копия не трогается
    unpack_bundle(db, settings, pid, b2)
    b3 = _upload(client, h, pid, "evil", {"depmap/x.zip": _zip({"../escape.txt": b"no"})})
    with pytest.raises(xf.FilesError):
        unpack_bundle(db, settings, pid, b3)
    assert not (root.parent / "escape.txt").exists() and (root / "deepmaps" / "Документ Заказ" / "index.html").exists()
    # испорченный исходник: хеш после распаковки не совпал с MANIFEST.json
    bad_manifest = {"files": [{"path": "map/a.gz", "source_bytes": 2, "source_sha256": "0" * 64}]}
    b4 = _upload(client, h, pid, "bad", {"map/a.gz": gzip.compress(b"zz"), "map/MANIFEST.json": json.dumps(bad_manifest).encode()})
    with pytest.raises(xf.FilesError, match="не совпали"):
        unpack_bundle(db, settings, pid, b4)
    assert (root / "map" / "config_nav.sqlite").exists()


def test_complete_bundle_unpacks_itself(env, monkeypatch):
    import threading

    from app.app.services.exchange_unpack import project_config_dir

    client, manager_token, _, db, pid, art = env
    monkeypatch.setenv("EXCHANGE_AUTO_UNPACK", "1")
    get_settings.cache_clear()
    _, h = _key(db, pid, art, "write")
    _upload(client, h, pid, "auto1", {"depmap/Справочник Тест.zip": _zip({"Справочник Тест/index.html": b"<html>"})}, "entity_deepmap")
    bad = _upload(client, h, pid, "auto_bad", {"depmap/y.zip": _zip({"../escape.txt": b"no"})})
    for t in threading.enumerate():
        if t.name.startswith("unpack-b"):
            t.join(30)
    root = project_config_dir(get_settings(), pid)
    assert (root / "deepmaps" / "Справочник Тест" / "index.html").read_bytes() == b"<html>"
    journal = client.get(f"/projects/{pid}/agent-exchange", headers={"Authorization": f"Bearer {manager_token}"}).json()["items"]
    unpacks = [i for i in journal if i.get("kind") == "files_unpack"]
    assert any(i.get("decision") == "accepted" for i in unpacks) and any(i.get("decision") == "rejected" for i in unpacks), unpacks
    assert bad


def test_unpack_nested_same_name_zips_and_long_paths(env):
    from app.app.services.exchange_unpack import project_config_dir, unpack_bundle

    client, _, _, db, pid, art = env
    _, h = _key(db, pid, art, "write")
    step = "etalon_УправлениеПоступлением_A1_" + "Ш" * 100 + ".html"  # часть пути 130+ знаков, полный путь за 260
    blobs = {f"depmap/scenario_etalon_Обработка_Кнопка{i}_" + "К" * 60 + "/Обработка Тест.zip":
             _zip({"Обработка Тест/" + step: f"кнопка {i}".encode(), "Обработка Тест/index.html": b"<html>"}) for i in (1, 2)}
    bid = _upload(client, h, pid, "scenarios", blobs, "entity_deepmap")
    out = unpack_bundle(db, get_settings(), pid, bid)
    assert len(out["deepmaps"]) == 2
    root = xf.long_path(project_config_dir(get_settings(), pid) / "deepmaps")
    for i in (1, 2):
        d = root / (f"scenario_etalon_Обработка_Кнопка{i}_" + "К" * 60) / "Обработка Тест"
        assert (d / step).read_bytes() == f"кнопка {i}".encode()
        assert len(str(d / step)) > 260


def test_new_agent_discovers_files_on_connect(env):
    from app.app.mcp.project_protocol import handle_jsonrpc

    client, _, _, db, pid, art = env
    _, hw = _key(db, pid, art, "write")
    reader, hr = _key(db, pid, art, None)  # новый ключ без особых прав
    err, body = _call(db, pid, reader, "files_catalog")
    assert not err and body["bundles"] == []
    init = handle_jsonrpc(db, get_settings(), pid, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, token=reader)
    assert "ФАЙЛЫ ПРОЕКТА" not in init["result"]["instructions"]
    readme = "# Карта\nКак подключить".encode()
    nav = b"x" * 300
    bid = _upload(client, hw, pid, "map_v1", {"map/README.md": readme, "map/config_nav.sqlite.gz": nav}, "config_map")
    # незавершённая посылка в каталог не попадает
    client.post(f"/mcp/projects/{pid}/files/bundles", headers=hw, json={"name": "open1", "files": [{"path": "z", "size": 5, "sha256": _sha(b"zzzzz")}]})
    init = handle_jsonrpc(db, get_settings(), pid, {"jsonrpc": "2.0", "id": 2, "method": "initialize"}, token=reader)
    assert "ФАЙЛЫ ПРОЕКТА: 1 посылок" in init["result"]["instructions"] and "files_catalog" in init["result"]["instructions"]
    err, over = _call(db, pid, reader, "project_overview")
    assert not err and over["files"]["bundles"] == 1 and over["files"]["items"][0]["name"] == "map_v1"
    cat = client.get(f"/mcp/projects/{pid}/files/catalog", headers=hr).json()
    assert [b["name"] for b in cat["bundles"]] == ["map_v1"]
    assert {f["path"]: f["bundle"] for f in cat["current_files"]} == {"map/README.md": "map_v1", "map/config_nav.sqlite.gz": "map_v1"}
    b = cat["bundles"][0]
    assert b["readme"]["text"].startswith("# Карта") and {f["path"] for f in b["files"]} == {"map/README.md", "map/config_nav.sqlite.gz"}
    assert client.get(f"/mcp/projects/{pid}/files/bundles/{bid}/data/map/config_nav.sqlite.gz", headers=hr).content == nav
