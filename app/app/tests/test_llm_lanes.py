"""Две полосы локальной модели и хранитель модели (docs/plans/LLM-LANES-PLAN.md)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.app.services import llm_keeper, llm_lanes
from app.app.services.llm_keeper import LmProfile

ROOT = Path(__file__).resolve().parents[3]


def _settings(tmp_path, **extra):
    base = dict(
        data_dir=tmp_path,
        llm_provider="lmstudio",
        lmstudio_model="m",
        lmstudio_base_url="http://127.0.0.1:1234/v1",
        lmstudio_chat_token_budget=22000,
        lmstudio_idle_unload_sec=300,
        lmstudio_load_ttl_sec=1800,
    )
    base.update(extra)
    return SimpleNamespace(**base)


# --- очередь чата -------------------------------------------------------------------------------------------------

def test_chat_queue_serves_strictly_in_arrival_order_one_at_a_time():
    queue = llm_lanes.ChatQueue()
    order, active, peak = [], [0], [0]
    guard = threading.Lock()
    tickets = [queue.take(("chat", 1, n)) for n in range(4)]

    def run(ticket, n):
        queue.wait_turn(ticket)
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        order.append(n)
        time.sleep(0.02)
        with guard:
            active[0] -= 1
        queue.done(ticket)

    threads = [threading.Thread(target=run, args=(t, n)) for n, t in reversed(list(enumerate(tickets)))]
    for th in threads:
        th.start()
    for th in threads:
        th.join(5)
    assert order == [0, 1, 2, 3]
    assert peak[0] == 1
    assert queue.waiting() == 0


def test_chat_queue_skips_abandoned_ticket_and_reports_position():
    queue = llm_lanes.ChatQueue()
    first = queue.take("a")
    gone = queue.take("b")
    third = queue.take("c")
    assert queue.position("a") == 0 and queue.position("c") == 2
    queue.done(gone)  # запрос оборвался, не дождавшись очереди
    assert queue.position("c") == 1
    queue.done(first)
    queue.wait_turn(third)  # не зависает на брошенном талоне
    assert queue.position("c") == 0


def test_chat_status_labels(monkeypatch):
    queue = llm_lanes.ChatQueue()
    monkeypatch.setattr(llm_lanes, "chat_queue", queue)
    assert llm_lanes.chat_status("x") == {"queued": False}
    t1 = queue.take("a")
    queue.take("x")
    assert llm_lanes.chat_status("x")["label"] == "В очереди к ИИ: перед вами 1"
    queue.done(t1)
    assert llm_lanes.chat_status("x")["label"] == "ИИ готовит ответ…"


def test_chat_turn_sets_chat_lane_only_inside():
    assert llm_lanes.current_lane() == llm_lanes.LANE_BACKGROUND
    with llm_lanes.chat_turn(("chat", 0, 0)):
        assert llm_lanes.current_lane() == llm_lanes.LANE_CHAT
    assert llm_lanes.current_lane() == llm_lanes.LANE_BACKGROUND


# --- запрос к модели ----------------------------------------------------------------------------------------------

def test_background_call_from_api_process_is_refused_in_strict_mode(monkeypatch, tmp_path):
    monkeypatch.setenv(llm_lanes.ROLE_ENV, "api")
    monkeypatch.setenv(llm_lanes.STRICT_ENV, "1")
    with pytest.raises(RuntimeError, match="очереди воркера"):
        with llm_lanes.llm_request(_settings(tmp_path), est_tokens=10, what="t"):
            pass
    with llm_lanes.chat_turn(("chat", 9, 9)):
        with llm_lanes.llm_request(_settings(tmp_path), est_tokens=10, what="t"):
            pass


def test_big_background_request_waits_for_chat_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_keeper, "load_profile", lambda: LmProfile("m", 56000, 2))
    settings = _settings(tmp_path)
    chat_lock = llm_lanes._file_lock(settings, llm_lanes.LANE_CHAT)
    chat_lock.acquire()
    entered = threading.Event()

    def big():
        with llm_lanes.llm_request(settings, est_tokens=40000, what="tz"):
            entered.set()

    th = threading.Thread(target=big)
    th.start()
    assert not entered.wait(0.5), "исключительный фоновый запрос не должен начаться, пока отвечает чат"
    chat_lock.release()
    assert entered.wait(5)
    th.join(5)


def test_ordinary_background_request_does_not_wait_for_chat(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_keeper, "load_profile", lambda: LmProfile("m", 56000, 2))
    settings = _settings(tmp_path)
    chat_lock = llm_lanes._file_lock(settings, llm_lanes.LANE_CHAT)
    chat_lock.acquire()
    entered = threading.Event()

    def small():
        with llm_lanes.llm_request(settings, est_tokens=9000, what="entities"):
            entered.set()

    th = threading.Thread(target=small)
    th.start()
    try:
        assert entered.wait(5), "фоновый запрос в своей доле окна идёт параллельно с чатом"
    finally:
        chat_lock.release()
        th.join(5)


def test_lanes_idle_sees_busy_lane(tmp_path):
    settings = _settings(tmp_path)
    assert llm_lanes.lanes_idle(settings) is True
    lock = llm_lanes._file_lock(settings, llm_lanes.LANE_BACKGROUND)
    holder = threading.Thread(target=lambda: (lock.acquire(), time.sleep(0.6), lock.release()))
    holder.start()
    time.sleep(0.2)
    assert llm_lanes.lanes_idle(settings) is False
    holder.join(5)


def test_estimate_tokens_is_conservative_for_russian():
    # замер 2026-10-05: ~3,1 символа на токен; оценка не должна занижать
    assert llm_lanes.estimate_tokens("а" * 31000) >= 10000


# --- хранитель модели ---------------------------------------------------------------------------------------------

def test_repo_profile_matches_default_settings_model():
    from app.app.config import Settings

    profile = llm_keeper.load_profile()
    assert profile.model_key == Settings.model_fields["lmstudio_model"].default
    assert profile.parallel == 2 and profile.context_length == 32768
    assert profile.preferred_quant == "Q4_K_XL"
    assert profile.mmproj == "mmproj-BF16.gguf"
    keys = {f["key"] for f in profile.load_fields}
    assert {"llm.load.llama.flashAttention", "llm.load.llama.kCacheQuantizationType",
            "llm.load.llama.speculativeDecoding.draftMtp"} <= keys
    thinking = next(
        f["value"] for f in profile.operation_fields
        if f.get("key") == "llm.prediction.reasoning.enableThinking"
    )
    assert thinking is False
    temp = next(
        f["value"] for f in profile.operation_fields
        if f.get("key") == "llm.prediction.temperature"
    )
    assert temp == 0.1


def test_profile_mismatch_allows_flash_attn_context_align():
    profile = LmProfile("m", 56000, 2)
    assert llm_keeper.aligned_context_length(56000) == 56064
    assert llm_keeper.profile_mismatch(profile, {"contextLength": 56000, "parallel": 2}) == []
    assert llm_keeper.profile_mismatch(profile, {"contextLength": 56064, "parallel": 2}) == []
    assert llm_keeper.profile_mismatch(profile, {"contextLength": 32000, "parallel": 2})
    assert llm_keeper.profile_mismatch(profile, {"contextLength": 56064, "parallel": 1})


def test_model_config_and_load_args_follow_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("LMSTUDIO_HOME", str(tmp_path))
    profile = LmProfile("m", 56000, 2, load_fields=[{"key": "llm.load.contextLength", "value": 1},
                                                    {"key": "llm.load.llama.flashAttention", "value": True}])
    config = llm_keeper.model_config(profile)
    load = {f["key"]: f["value"] for f in config["load"]["fields"]}
    assert load["llm.load.contextLength"] == 56000 and load["llm.load.numParallelSessions"] == 2
    assert load["llm.load.llama.flashAttention"] is True
    path = llm_keeper.write_model_config(profile, "pub/repo/file.gguf")
    assert path == tmp_path / ".internal" / "user-concrete-model-default-config" / "pub" / "repo" / "file.gguf.json"
    assert json.loads(path.read_text(encoding="utf-8")) == config
    args = llm_keeper.load_args(profile, ttl_sec=1800)
    assert args[:2] == ["load", "m"]
    assert args[args.index("--context-length") + 1] == "56000" and args[args.index("--parallel") + 1] == "2"
    assert "--speculative-draft-mtp" in args
    assert args[args.index("--speculative-draft-max-tokens") + 1] == "3"
    assert "--ttl" in args


class FakeLms:
    """Имитация `lms`: ls / ps / load / unload."""

    def __init__(self, loaded=None):
        self.loaded = loaded
        self.calls = []

    def __call__(self, args, timeout=60):
        self.calls.append(args[0])
        out = ""
        if args[0] == "ls":
            out = json.dumps([{"modelKey": "m", "indexedModelIdentifier": "pub/m.gguf", "sizeBytes": 1}])
        elif args[0] == "ps":
            out = json.dumps([self.loaded] if self.loaded else [])
        elif args[0] == "load":
            ctx = int(args[args.index("--context-length") + 1])
            self.loaded = {"identifier": "m", "contextLength": ctx, "parallel": int(args[args.index("--parallel") + 1])}
        elif args[0] == "unload":
            self.loaded = None
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


@pytest.fixture
def keeper_env(monkeypatch, tmp_path):
    monkeypatch.delenv(llm_keeper.DISABLE_ENV, raising=False)
    monkeypatch.setenv("LMSTUDIO_HOME", str(tmp_path / "lms"))
    monkeypatch.setattr(llm_keeper, "load_profile", lambda: LmProfile("m", 56000, 2))
    monkeypatch.setattr(llm_keeper, "_wait_vram_for_load", lambda *_a: None)
    monkeypatch.setattr(llm_keeper, "_after_load", lambda _s: None)
    llm_keeper._forget_verified()
    return _settings(tmp_path)


def test_ensure_loaded_loads_by_profile(monkeypatch, keeper_env):
    lms = FakeLms()
    monkeypatch.setattr(llm_keeper, "_lms", lms)
    monkeypatch.setattr(llm_keeper, "model_state", lambda _s: "not-loaded")
    llm_keeper.ensure_loaded(keeper_env)
    assert lms.loaded == {"identifier": "m", "contextLength": 56000, "parallel": 2}
    assert "load" in lms.calls


def test_ensure_loaded_reloads_model_loaded_with_wrong_settings(monkeypatch, keeper_env):
    lms = FakeLms({"identifier": "m", "contextLength": 32000, "parallel": 1})  # так её поднимала автозагрузка
    monkeypatch.setattr(llm_keeper, "_lms", lms)
    monkeypatch.setattr(llm_keeper, "model_state", lambda _s: "loaded")
    llm_keeper.ensure_loaded(keeper_env)
    assert lms.calls.count("unload") == 1 and lms.calls.count("load") == 1
    assert lms.loaded["parallel"] == 2


def test_ensure_loaded_refuses_model_that_loads_off_profile(monkeypatch, keeper_env):
    class Stubborn(FakeLms):
        def __call__(self, args, timeout=60):
            res = super().__call__(args, timeout)
            if args[0] == "load":
                self.loaded["parallel"] = 1  # LM Studio проигнорировал флаг
            return res

    lms = Stubborn()
    monkeypatch.setattr(llm_keeper, "_lms", lms)
    monkeypatch.setattr(llm_keeper, "model_state", lambda _s: "not-loaded")
    with pytest.raises(llm_keeper.LlmLoadError, match="не по профилю"):
        llm_keeper.ensure_loaded(keeper_env)
    assert lms.loaded is None  # выгружена, не используется


def test_ensure_loaded_rejects_settings_model_not_in_profile(monkeypatch, keeper_env):
    keeper_env.lmstudio_model = "другая"
    monkeypatch.setattr(llm_keeper, "model_state", lambda _s: "not-loaded")
    with pytest.raises(llm_keeper.LlmLoadError, match="не совпадает с профилем"):
        llm_keeper.ensure_loaded(keeper_env)


def test_idle_unload_only_after_idle_and_free_lanes(monkeypatch, keeper_env):
    lms = FakeLms({"identifier": "m", "contextLength": 56000, "parallel": 2})
    monkeypatch.setattr(llm_keeper, "_lms", lms)
    monkeypatch.setattr(llm_keeper, "model_state", lambda _s: "loaded" if lms.loaded else "not-loaded")
    llm_keeper.touch_last_use(keeper_env)
    assert llm_keeper.maybe_unload_idle(keeper_env) is False  # только что работала
    stamp = keeper_env.data_dir / "locks" / llm_keeper.LAST_USE_FILE
    stamp.write_text(str(time.time() - 301), encoding="utf-8")
    monkeypatch.setattr(llm_lanes, "lanes_idle", lambda _s: False)
    assert llm_keeper.maybe_unload_idle(keeper_env) is False  # чат отвечает
    monkeypatch.setattr(llm_lanes, "lanes_idle", lambda _s: True)
    assert llm_keeper.maybe_unload_idle(keeper_env) is True
    assert lms.loaded is None


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([{"id": "m", "state": "loaded"}], "loaded"),
        ([{"id": "m", "state": "not-loaded"}, {"id": "x", "state": "loaded"}], "not-loaded"),  # JIT включён: все скачанные
        ([], "not-loaded"),  # JIT выключен: незагруженной модели в списке нет
    ],
)
def test_model_state_reads_rest_v0_list(monkeypatch, tmp_path, rows, expected):
    seen = {}

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": rows}

    class Client:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url):
            seen["url"] = url
            return Resp()

    monkeypatch.setattr(llm_keeper.httpx, "Client", Client)
    assert llm_keeper.model_state(_settings(tmp_path)) == expected
    assert seen["url"] == "http://127.0.0.1:1234/api/v0/models"


def test_model_state_none_when_lm_studio_is_down(monkeypatch, tmp_path):
    class Client:
        def __init__(self, timeout):
            raise llm_keeper.httpx.ConnectError("refused")

    monkeypatch.setattr(llm_keeper.httpx, "Client", Client)
    assert llm_keeper.model_state(_settings(tmp_path)) is None


def test_no_raw_lms_load_outside_keeper():
    """Модель поднимается только хранителем по профилю: прямой `lms load` в коде портала поднял бы её не так."""
    offenders = []
    for path in (ROOT / "app" / "app").rglob("*.py"):
        if "tests" in path.parts or path.name == "llm_keeper.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if '"lms", "load"' in text or "'lms', 'load'" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


# --- кнопки портала — задачами воркера -----------------------------------------------------------------------------

def _project_with_artifact(client, token, name):
    from sqlalchemy import select

    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.user import User

    pid = client.post("/projects", json={"name": name}, headers={"Authorization": f"Bearer {token}"}).json()["id"]
    db = SessionLocal()
    try:
        user = db.scalar(select(User).limit(1))
        art = Artifact(
            artifact_id=f"art_{name}", project_id=pid, user_id=user.id, original_filename="call.webm",
            stored_filename="call.webm", file_path="x", file_type="audio", source_type="call", status="done",
            sha256="abc", transcription_text="[speaker_0] план к пятнице",
        )
        db.add(art)
        db.commit()
        return pid, art.artifact_id
    finally:
        db.close()


def test_api_buttons_that_need_model_are_queued_for_worker(client, admin_token, manager_token):
    pid, art_key = _project_with_artifact(client, manager_token, "Buttons")
    admin = {"Authorization": f"Bearer {admin_token}"}
    card = client.post(f"/projects/{pid}/card/rebuild", json={}, headers=admin)
    assert card.status_code == 200 and card.json()["queued"] is True
    tz = client.post(f"/projects/{pid}/rebuild-tz", headers=admin)
    assert tz.status_code == 200 and tz.json()["queued"] is True
    assert client.post(f"/projects/{pid}/rebuild-tz", headers=admin).status_code == 409  # уже в очереди
    summary = client.post(f"/api/artifacts/{art_key}/rebuild-summary", headers=admin)
    assert summary.status_code == 200, summary.text
    assert summary.json()["queued"] is True
    kinds = {t["task_type"] for t in client.get(f"/projects/{pid}/tasks/active", headers=admin).json()["tasks"]}
    assert kinds == {"card_rebuild", "tz_rebuild", "call_summary_rebuild"}


def test_chat_double_submit_creates_one_question(client, manager_token, monkeypatch):
    """Два одновременных вопроса одного пользователя: проверка «уже ждёт» и запись — под одним замком."""
    from app.app.services import chat

    pid, _ = _project_with_artifact(client, manager_token, "DoubleSubmit")
    gate = threading.Event()
    monkeypatch.setattr(chat, "_answer_chat_inner", lambda *a, **k: gate.wait(5) and {"answer": "ok", "sources": []})
    headers = {"Authorization": f"Bearer {manager_token}"}
    codes = []

    def ask():
        codes.append(client.post("/chat", json={"project_id": pid, "question": "Срок?"}, headers=headers).status_code)

    threads = [threading.Thread(target=ask) for _ in range(2)]
    for th in threads:
        th.start()
    time.sleep(0.5)
    gate.set()
    for th in threads:
        th.join(10)
    assert sorted(codes) == [200, 409]


# --- модель и ASR на GPU -------------------------------------------------------------------------------------------

def test_asr_gpu_hold_waits_for_chat_answer_then_unloads_and_blocks_loading(monkeypatch, keeper_env):
    lms = FakeLms({"identifier": "m", "contextLength": 56000, "parallel": 2})
    monkeypatch.setattr(llm_keeper, "_lms", lms)
    monkeypatch.setattr(llm_keeper, "model_state", lambda _s: "loaded" if lms.loaded else "not-loaded")
    monkeypatch.setattr("app.app.services.gpu_phase.lms_unload_all", lambda: None)
    keeper_env.lmstudio_keep_during_asr = False
    chat_lock = llm_lanes._file_lock(keeper_env, llm_lanes.LANE_CHAT)
    chat_lock.acquire()  # чат отвечает
    inside, release, loaded_during = threading.Event(), threading.Event(), []

    def step():
        with llm_keeper.asr_gpu_hold(keeper_env, step="lab_giga"):
            inside.set()
            release.wait(5)

    th = threading.Thread(target=step)
    th.start()
    assert not inside.wait(0.5), "GPU-шаг ждёт, пока чат договорит"
    assert llm_keeper.asr_hold_active(keeper_env)
    chat_lock.release()
    assert inside.wait(5)
    assert lms.loaded is None  # модель выгружена на время шага

    def chat_wants_model():
        llm_keeper.ensure_loaded(keeper_env)
        loaded_during.append(release.is_set())

    th2 = threading.Thread(target=chat_wants_model)
    th2.start()
    time.sleep(0.5)
    assert loaded_during == [] and lms.loaded is None, "пока идёт шаг, модель не загружается"
    release.set()
    th.join(5)
    th2.join(5)
    assert loaded_during == [True] and lms.loaded["parallel"] == 2
    assert not llm_keeper.asr_hold_active(keeper_env)


@pytest.mark.parametrize(("step", "keep"), [("lab_proof", False), ("lab_reconcile", False), ("lab_giga", True)])
def test_asr_gpu_hold_skips_non_gpu_steps_and_keep_mode(monkeypatch, keeper_env, step, keep):
    lms = FakeLms({"identifier": "m", "contextLength": 56000, "parallel": 2})
    monkeypatch.setattr(llm_keeper, "_lms", lms)
    monkeypatch.setattr(llm_keeper, "model_state", lambda _s: "loaded")
    keeper_env.lmstudio_keep_during_asr = keep
    with llm_keeper.asr_gpu_hold(keeper_env, step=step):
        assert lms.loaded is not None
    assert "unload" not in lms.calls


def test_chat_status_says_asr_is_running(monkeypatch, tmp_path):
    queue = llm_lanes.ChatQueue()
    monkeypatch.setattr(llm_lanes, "chat_queue", queue)
    monkeypatch.setattr(llm_lanes, "_asr_hold_active", lambda: True)
    ticket = queue.take("x")
    queue.set_loading(ticket, True)
    assert "распознавание речи" in llm_lanes.chat_status("x")["label"]
