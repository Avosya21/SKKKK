"""API ручных действий над артефактом (выжимка, переиндекс)."""


def test_rebuild_summary_route_registered(client):
    r = client.post("/api/artifacts/missing-artifact/rebuild-summary")
    assert r.status_code == 404


def test_reindex_route_registered(client):
    r = client.post("/api/artifacts/missing-artifact/reindex")
    assert r.status_code == 404
