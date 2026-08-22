from app.app.services.report_embed import apply_embed_mode


def test_apply_embed_mode_audio_hides_title():
    html = "<html><head></head><body><header><h1>Title</h1></header></body></html>"
    out = apply_embed_mode(html, kind="audio")
    assert "skkkk-embed" in out
    assert "display: none" in out


def test_apply_embed_mode_document_flattens_cards():
    html = "<html><head></head><body><h1>Doc</h1><section class='card'></section></body></html>"
    out = apply_embed_mode(html, kind="document")
    assert "skkkk-embed" in out
    assert ".card" in out
