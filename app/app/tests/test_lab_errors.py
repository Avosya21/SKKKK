from app.app.services.lab_errors import NO_SPEECH, sanitize_lab_error


def test_sanitize_drops_onnx_warning_and_keeps_no_speech():
    raw = (
        "\x1b[0;93m2026-09-13 [W:onnxruntime:, transformer_memcpy.cc:111 "
        "onnxruntime::MemcpyTransformer::ApplyImpl] 2 Memcpy nodes\x1b[m "
        "RuntimeError: onnx-asr не собрал ни одного слова — покрасить спикерами будет нечем"
    )
    assert sanitize_lab_error(raw, step="lab_onnx") == NO_SPEECH


def test_sanitize_keeps_plain_runtime_error():
    raw = "lab_giga failed:\nRuntimeError: контейнер не ответил"
    assert sanitize_lab_error(raw, step="lab_giga") == "контейнер не ответил"


def test_sanitize_gigaam_connection_refused():
    from app.app.services.lab_errors import GIGAAM_DOWN

    raw = (
        'lab_giga: rene_\\AppData\\Local\\Programs\\Python\\Python310\\lib\\http\\client.py", '
        "line 941, in connect self.sock = self._create_connection( "
        "File socket.py, line 833, in create_connection sock.connect(sa) ConnectionRefusedError"
    )
    assert sanitize_lab_error(raw, step="lab_giga") == GIGAAM_DOWN


def test_sanitize_empty_replicas_is_no_speech():
    raw = "RuntimeError: из покрашенных слов не собралось ни одной реплики"
    assert sanitize_lab_error(raw, step="lab_onnx") == NO_SPEECH


def test_sanitize_gigaam_not_started_message():
    from app.app.services.lab_errors import GIGAAM_DOWN

    raw = "GigaAM не запущен — контейнер skkkk-gigaam (http://127.0.0.1:18102)"
    assert sanitize_lab_error(raw) == GIGAAM_DOWN
