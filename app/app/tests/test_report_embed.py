from app.app.services.report_embed import apply_embed_mode, apply_role_chrome


def test_apply_embed_mode_audio_hides_title():
    html = "<html><head></head><body><header><h1>Title</h1></header></body></html>"
    out = apply_embed_mode(html, kind="audio")
    assert "skkkk-embed" in out
    assert "display: none" in out


def test_apply_embed_mode_document_flattens_cards():
    html = "<html><head></head><body><header><h1>Doc</h1></header><section class='summary'></section></body></html>"
    out = apply_embed_mode(html, kind="document")
    assert "skkkk-embed" in out
    assert ".summary" in out


def test_apply_role_chrome_hides_rebuild_for_user():
    html = "<html><head></head><body><button id='rebuild-summary'></button></body></html>"
    out = apply_role_chrome(html, "user")
    assert "SKKKK_ROLE='user'" in out or 'SKKKK_ROLE="user"' in out
    assert "skkkk-role-chrome" in out
    admin = apply_role_chrome(html, "admin")
    assert "skkkk-role-chrome" not in admin
