def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "queue" not in data
    assert "vram_mib" not in data
    assert "llm_provider" not in data


def test_health_ready_requires_auth(anon_client):
    r = anon_client.get("/health/ready")
    assert r.status_code == 401


def test_health_ready_with_session(client, admin_token):
    r = client.get("/health/ready")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "queue" in data
    assert "llm_provider" in data
