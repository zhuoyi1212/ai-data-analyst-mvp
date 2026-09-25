from fastapi.testclient import TestClient

from app.main import app


def test_health() -> None:
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["fixture_mode"] in {"replay", "live", "record"}
