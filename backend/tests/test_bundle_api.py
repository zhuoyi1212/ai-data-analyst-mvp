"""重构 Phase 1：AnalysisBundle 路由端到端测试（FastAPI TestClient，离线）。"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from test_bundle_planner import _ready_session

client = TestClient(app)


@pytest.fixture
def api_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """让路由内部的 SessionStore() 落到临时目录。"""
    test_settings = dataclasses.replace(settings, storage_dir=tmp_path / "store")
    import app.services.storage as storage_mod
    monkeypatch.setattr(storage_mod, "settings", test_settings)
    return test_settings.storage_dir


def test_bundle_generate_and_execute(api_store, tmp_path: Path):
    # 用测试助手在与路由相同的临时存储根中准备就绪会话
    _, sid, _, snapshot = _ready_session("sales_orders", tmp_path)
    assert (api_store / sid).exists()  # 助手与路由共用同一存储根

    # 生成 Bundle
    resp = client.post(f"/sessions/{sid}/analysis-bundle")
    assert resp.status_code == 200, resp.text
    bundle = resp.json()
    # T07：Seed 3-5 个 presentation，加上 computation 证据任务总数 ≤ 10
    assert 3 <= len(bundle["analysis_views"]) <= 10
    assert 3 <= sum(
        v["role"] == "presentation" for v in bundle["analysis_views"]
    ) <= 5
    assert bundle["bundle_id"]

    # GET 可重新读取（页面恢复）
    got = client.get(f"/sessions/{sid}/analysis-bundle")
    assert got.status_code == 200
    assert got.json()["bundle_id"] == bundle["bundle_id"]

    # 批量执行
    resp = client.post(f"/sessions/{sid}/analysis-bundle/execute")
    assert resp.status_code == 200, resp.text
    execution = resp.json()
    assert execution["total"] == len(bundle["analysis_views"])
    assert execution["failed"] == 0
    assert execution["succeeded"] == execution["total"]
    for v in execution["views"]:
        assert v["status"] == "success"
        assert v["result_rows_total"] > 0
        assert v["checks"]

    # 执行汇总 GET 可读
    got = client.get(f"/sessions/{sid}/analysis-bundle/execution")
    assert got.status_code == 200
    assert got.json()["succeeded"] == execution["succeeded"]


def test_bundle_requires_completed_semantics(api_store):
    """语义未确认/未做质量处理时，规划接口 409。"""
    from app.services.storage import SessionStore
    sid = SessionStore().create_from_sample("sales_orders.csv")["session_id"]
    resp = client.post(f"/sessions/{sid}/analysis-bundle")
    assert resp.status_code == 409
