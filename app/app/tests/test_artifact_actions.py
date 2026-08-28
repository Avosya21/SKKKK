"""API ручных действий над артефактом (выжимка, переиндекс)."""


def test_rebuild_summary_requires_auth(anon_client):
    r = anon_client.post("/api/artifacts/missing-artifact/rebuild-summary")
    assert r.status_code == 401


def test_rebuild_summary_route_registered(client, admin_token):
    r = client.post(
        "/api/artifacts/missing-artifact/rebuild-summary",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 404


def test_reindex_requires_auth(anon_client):
    r = anon_client.post("/api/artifacts/missing-artifact/reindex")
    assert r.status_code == 401


def test_reindex_route_registered(client, admin_token):
    r = client.post(
        "/api/artifacts/missing-artifact/reindex",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 404
