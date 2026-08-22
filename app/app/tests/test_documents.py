from pathlib import Path

from app.app.services.normalize import light_normalize
from app.app.services.pipeline_documents import _extract_html, _extract_plain, _extract_text, _strip_page_numbers
from app.app.services.upload import detect_file_type


def test_txt_normalization_preserves_content():
    raw = "Сумма  1 500 000 руб.\n\nДата 19.08.2026"
    out = light_normalize(raw)
    assert "1 500 000" in out
    assert "19.08.2026" in out


def test_strip_page_numbers():
    text = "Страница\n12\n\nТекст документа"
    out = _strip_page_numbers(text)
    assert "12" not in out.split()
    assert "Текст документа" in out


def test_empty_document_detection():
    assert not "   ".strip()


def test_detect_file_type_html():
    assert detect_file_type("report.html", "text/html") == "html"
    assert detect_file_type("page.htm", None) == "html"


def test_detect_file_type_text_files():
    assert detect_file_type("note.txt", "text/plain") == "txt"
    assert detect_file_type("readme.md", None) == "txt"
    assert detect_file_type("data.csv", "text/csv") == "txt"
    assert detect_file_type("legacy.rtf", "application/rtf") == "txt"
    assert detect_file_type("config.json", "application/json") == "txt"
    assert detect_file_type("noext", "text/plain") == "txt"


def test_extract_text_utf8_and_cp1251(tmp_path: Path):
    utf8 = tmp_path / "utf8.txt"
    utf8.write_text("Привет, мир\nВторая строка", encoding="utf-8")
    assert "Привет" in _extract_text(utf8)
    assert "Вторая строка" in _extract_text(utf8)

    cp1251 = tmp_path / "win.txt"
    cp1251.write_bytes("Сумма 1 500 000 руб.".encode("cp1251"))
    assert "1 500 000" in _extract_text(cp1251)


def test_extract_plain_rtf(tmp_path: Path):
    rtf = tmp_path / "sample.rtf"
    rtf.write_text(
        r"{\rtf1\ansi\deff0 {\fonttbl{\f0 Times New Roman;}}\f0\par "
        r"\'cf\'f0\'e8\'e2\'e5\'f2\par \par \line \tab ok}",
        encoding="latin-1",
    )
    raw = _extract_plain(rtf)
    assert "ok" in raw


def test_extract_html_strips_markup(tmp_path: Path):
    html_path = tmp_path / "sample.html"
    title = "\u0417\u0430\u0433\u043e\u043b\u043e\u0432\u043e\u043a"
    body = "\u0422\u0435\u043a\u0441\u0442 \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u0430"
    html_path.write_text(
        "<html><head><title>T</title><style>.x{}</style></head>"
        f"<body><h1>{title}</h1><p>{body} <b>bold</b>.</p>"
        "<script>alert(1)</script></body></html>",
        encoding="utf-8",
    )
    raw = _extract_html(html_path)
    assert title in raw
    assert body in raw
    assert "alert" not in raw
