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


def test_detect_file_type_pptx():
    assert detect_file_type("deck.pptx", None) == "pptx"
    assert detect_file_type("old.ppt", "application/vnd.ms-powerpoint") == "pptx"
    assert detect_file_type("deck", "application/vnd.openxmlformats-officedocument.presentationml.presentation") == "pptx"


def _make_deck(path: Path) -> None:
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[1])
    s1.shapes.title.text = "Бюджет проекта"
    s1.placeholders[1].text_frame.text = "Итого 1 500 000 руб."
    s1.notes_slide.notes_text_frame.text = "Уточнить у заказчика"
    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    s2.shapes.title.text = "Сроки"
    tbl = s2.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(6), Inches(1)).table
    for (r, c), v in {(0, 0): "Этап", (0, 1): "Дата", (1, 0): "Пилот", (1, 1): "19.08.2026"}.items():
        tbl.cell(r, c).text = v
    cd = CategoryChartData()
    cd.categories = ["Q1", "Q2"]
    cd.add_series("План", (10, 20))
    s2.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(4), Inches(5), Inches(3), cd)
    prs.save(str(path))


def test_extract_pptx_slides_tables_notes_charts(tmp_path: Path):
    from app.app.services.pptx_extract import extract_pptx

    deck = tmp_path / "deck.pptx"
    _make_deck(deck)
    out = extract_pptx(deck)
    assert "## Слайд 1: Бюджет проекта" in out
    assert "1 500 000" in out
    assert "Заметки докладчика:\nУточнить у заказчика" in out
    assert "## Слайд 2: Сроки" in out
    assert "| Пилот | 19.08.2026 |" in out
    assert "План: Q1: 10; Q2: 20" in out


def test_extract_legacy_ppt_via_libreoffice(tmp_path: Path):
    import subprocess

    import pytest

    from app.app.services.pptx_extract import _find_soffice, extract_pptx

    soffice = _find_soffice()
    if not soffice:
        pytest.skip("LibreOffice не установлен")
    deck = tmp_path / "deck.pptx"
    _make_deck(deck)
    ppt_dir = tmp_path / "ppt"
    ppt_dir.mkdir()
    subprocess.run([soffice, f"-env:UserInstallation={(tmp_path / 'p').resolve().as_uri()}", "--headless",
                    "--convert-to", "ppt", "--outdir", str(ppt_dir), str(deck)], capture_output=True, timeout=180)
    ppt = ppt_dir / "deck.ppt"
    assert ppt.exists()
    out = extract_pptx(ppt)
    assert "Бюджет проекта" in out and "1 500 000" in out
