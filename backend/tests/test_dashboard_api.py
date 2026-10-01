"""Phase 2：DashboardArtifact 路由测试（门禁 409/404、合成与 GET 恢复）。"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.schemas.dashboard import DashboardArtifact
from test_bundle_planner import _ready_session

client = TestClient(app)


@pytest.fixture
def api_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    test_settings = dataclasses.replace(settings, storage_dir=tmp_path / "store")
    import app.services.storage as storage_mod
    monkeypatch.setattr(storage_mod, "settings", test_settings)
    return test_settings.storage_dir


def _bundle_executed(sid: str) -> dict:
    r = client.post(f"/sessions/{sid}/analysis-bundle")
    assert r.status_code == 200, r.text
    r = client.post(f"/sessions/{sid}/analysis-bundle/execute")
    assert r.status_code == 200, r.text
    return r.json()


def test_dashboard_requires_bundle_then_execution(api_store, tmp_path: Path):
    _, sid, _, _ = _ready_session("sales_orders", tmp_path)
    # 无 bundle → 409
    assert client.post(f"/sessions/{sid}/dashboard").status_code == 409
    # 有 bundle 未执行 → 409
    assert client.post(f"/sessions/{sid}/analysis-bundle").status_code == 200
    assert client.post(f"/sessions/{sid}/dashboard").status_code == 409


def test_dashboard_missing_session_404(api_store):
    assert client.get("/sessions/no-such-session/dashboard").status_code == 404
    assert client.post("/sessions/no-such-session/dashboard").status_code == 404


def test_dashboard_synthesize_and_get(api_store, tmp_path: Path):
    _, sid, _, _ = _ready_session("sales_orders", tmp_path)
    execution = _bundle_executed(sid)

    resp = client.post(f"/sessions/{sid}/dashboard")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    artifact = DashboardArtifact.model_validate(payload)  # 契约可解析
    assert artifact.bundle_id
    assert artifact.run_id  # T01：产物携带运行版本
    assert len(artifact.kpis) >= 1
    assert [s.section_id for s in artifact.sections] == [
        "overview", "trend", "structure", "diagnosis", "detail"
    ]
    # T04：只有 status=success 且可消费（pass/warn）的 View 才能进 sections；
    # T07：sections 只收 presentation，computation 证据任务不占默认视图
    bundle_resp = client.get(f"/sessions/{sid}/analysis-bundle")
    presentation_ids = {
        v["view_id"] for v in bundle_resp.json()["analysis_views"]
        if v["role"] == "presentation"
    }
    consumable_ids = {
        v["view_id"] for v in execution["views"] if v.get("consumable", True)
    }
    slotted = {vid for s in artifact.sections for vid in s.view_ids}
    assert slotted == consumable_ids & presentation_ids

    # GET 恢复 + dashboard.json 按运行版本落盘（runs/{run_id}/）
    got = client.get(f"/sessions/{sid}/dashboard")
    assert got.status_code == 200
    assert got.json()["bundle_id"] == artifact.bundle_id
    path = api_store / sid / "bundle" / "runs" / artifact.run_id / "dashboard.json"
    assert path.exists()
