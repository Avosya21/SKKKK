from app.app.config import Settings, external_base_url


def test_external_base_url_prefers_web_public_url():
    settings = Settings(
        secret_key="test-secret-key-32chars!!",
        web_public_url="http://203.0.113.10:777",
        web_host="0.0.0.0",
        web_port=777,
    )
    assert external_base_url(settings, "http://127.0.0.1:777/") == "http://203.0.113.10:777"


def test_external_base_url_falls_back_to_request():
    settings = Settings(secret_key="test-secret-key-32chars!!", web_public_url="")
    assert external_base_url(settings, "http://example.test:9000/") == "http://example.test:9000"
