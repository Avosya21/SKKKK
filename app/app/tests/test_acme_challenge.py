from unittest.mock import patch

import httpx


def test_acme_challenge_is_public_and_proxies(client):
    fake = httpx.Response(200, text="token-body", headers={"content-type": "text/plain"})
    with patch("app.app.services.acme_challenge.httpx.get", return_value=fake) as get:
        response = client.get("/.well-known/acme-challenge/abc_DEF-123")
    assert response.status_code == 200
    assert response.text == "token-body"
    get.assert_called_once()
    url = get.call_args.args[0]
    assert url.endswith("/.well-known/acme-challenge/abc_DEF-123")


def test_acme_challenge_rejects_junk_token(client):
    response = client.get("/.well-known/acme-challenge/bad.token")
    assert response.status_code == 404
