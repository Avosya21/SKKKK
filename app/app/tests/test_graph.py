from app.app.services.graph import CANCEL_PATTERNS


def test_cancel_pattern_matches():
    assert CANCEL_PATTERNS.search("Мы отменяем прежнее решение по бюджету")


def test_build_graph_payload_empty(client, db_engine, admin_token):
    r = client.post("/projects", json={"name": "G", "description": ""}, headers={"Authorization": f"Bearer {admin_token}"})
    pid = r.json()["id"]
    r2 = client.get(f"/projects/{pid}/graph/data", headers={"Authorization": f"Bearer {admin_token}"})
    assert r2.status_code == 200
    data = r2.json()
    assert "nodes" in data and "edges" in data
