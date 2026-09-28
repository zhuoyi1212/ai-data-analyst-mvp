"""P1 T06：前端可消费的 Dashboard 契约测试。

验收锚点：
- 浏览器只靠 API 即可绘图，不接触服务器 parquet：ViewCard 内嵌 ChartSpec、
  数据 schema、可直接绘图的行（或分页/采样元数据 + data_ref）；
- ChartSpec x/y 字段必须存在于输出 schema，字段错配拒绝；
- 全局筛选改变时重算聚合/比率/Findings，筛选后可见内容一致；
- 提供历史运行、部分失败、陈旧结果和空态。
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.schemas.dashboard import DashboardArtifact
from app.services.bundle_executor import execute_bundle
from app.services.bundle_planner import build_bundle  # noqa: F401 (symmetry check)
from app.services.dashboard_synthesizer import synthesize_dashboard
from app.services.storage import SessionStore
from test_bundle_planner import _ready_session
from test_dashboard_api import _bundle_executed

client = TestClient(app)


@pytest.fixture
def api_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    test_settings = dataclasses.replace(settings, storage_dir=tmp_path / "store")
    import app.services.storage as storage_mod
    monkeypatch.setattr(storage_mod, "settings", test_settings)
    return test_settings.storage_dir


def _published(tmp_path: Path) -> tuple[SessionStore, str]:
    store, sid, dictionary, snapshot = _ready_session("sales_orders", tmp_path)
    bundle = build_bundle(
        dictionary, snapshot, snapshot_rows=len(snapshot), title="sales_orders.csv"
    )
    store.write_bundle(sid, bundle.model_dump(mode="json"))
    execute_bundle(sid, store, bundle)
    synthesize_dashboard(sid, store)
    return store, sid


# ----------------------------------------------------------------- 视图字典

def test_view_cards_are_self_sufficient(tmp_path: Path):
    store, sid = _published(tmp_path)
    artifact = DashboardArtifact.model_validate(store.read_dashboard(sid))

    assert artifact.state in {"ready", "partial"}
    assert artifact.views, "artifact 必须内嵌视图字典"
    for card in artifact.views.values():
        if card.consumable:
            assert card.data is not None
            col_names = {c.name for c in card.data.columns}
            # schema + 行齐备，每行只引用 schema 中的列
            for row in card.data.rows:
                assert set(row) <= col_names
            if card.chart_spec is not None:
                if card.chart_spec.x_field is not None:
                    assert card.chart_spec.x_field in col_names
                for y in card.chart_spec.y_fields:
                    assert y in col_names
        else:
            assert card.reason  # 不可消费必须给中文原因，绝不静默

    # scope：快照行数与筛选后参与行数（无筛选时相等）
    assert artifact.scope.snapshot_rows > 0
    assert artifact.scope.participating_rows == artifact.scope.snapshot_rows


def test_scatter_views_carry_sample_metadata(tmp_path: Path):
    """散点视角：采样只影响展示，统计量基于全量样本。"""
    store, sid, dictionary, snapshot = _ready_session("sales_orders", tmp_path)
    bundle = build_bundle(
        dictionary, snapshot, snapshot_rows=len(snapshot), title="sales_orders.csv"
    )
    store.write_bundle(sid, bundle.model_dump(mode="json"))
    execute_bundle(sid, store, bundle)
    artifact = synthesize_dashboard(sid, store)

    scatters = [c for c in artifact.views.values() if c.type == "relationship"]
    for card in scatters:
        assert card.data is not None and card.data.sample is not None
        sample = card.data.sample
        assert sample.total_count >= sample.display_count
        assert sample.sample_method in {"full", "even_stride"}


# ----------------------------------------------------------------- 筛选重算

def test_refine_recomputes_everything_under_scope(tmp_path: Path):
    store, sid = _published(tmp_path)
    before = DashboardArtifact.model_validate(store.read_dashboard(sid))
    f = before.global_filters[0]
    member = f.members[0]

    snapshot = store.load_snapshot(sid)
    from app.schemas.bundle import AnalysisBundle
    from app.services.bundle_planner import refine_bundle
    source = AnalysisBundle.model_validate(store.read_bundle(sid))
    scope = [{"column": f.column, "values": [member]}]
    new_bundle = refine_bundle(source, snapshot, scope)
    store.write_bundle(
        sid, new_bundle.model_dump(mode="json"), scope_filters=scope
    )
    execute_bundle(sid, store, new_bundle)
    after = synthesize_dashboard(sid, store)

    # 范围已裁剪：参与行数 < 快照行数
    assert after.scope.participating_rows < after.scope.snapshot_rows
    assert after.scope.filters[0].column == f.column
    # FilterDefinition 回显当前已选值
    refined_filter = next(x for x in after.global_filters if x.column == f.column)
    assert refined_filter.selected_values == [member]

    # KPI 与筛选后快照真实重算一致（金额为 sum 口径）
    scope_df = snapshot[snapshot[f.column].astype(str) == member]
    expected_amount = float(scope_df["金额"].sum())
    amount_kpi = next(k for k in after.kpis if k.label == "金额")
    assert abs(amount_kpi.value - expected_amount) < 1e-6


def test_refine_zero_match_is_empty_state_without_fake_zero(tmp_path: Path):
    store, sid = _published(tmp_path)
    snapshot = store.load_snapshot(sid)
    from app.schemas.bundle import AnalysisBundle
    from app.services.bundle_planner import refine_bundle
    before = DashboardArtifact.model_validate(store.read_dashboard(sid))
    f = before.global_filters[0]

    source = AnalysisBundle.model_validate(store.read_bundle(sid))
    scope = [{"column": f.column, "values": ["__不存在的成员__"]}]
    new_bundle = refine_bundle(source, snapshot, scope)
    store.write_bundle(sid, new_bundle.model_dump(mode="json"), scope_filters=scope)
    execute_bundle(sid, store, new_bundle)
    after = synthesize_dashboard(sid, store)

    assert after.state == "empty"
    assert after.scope.participating_rows == 0
    # 绝不伪造 0：没有任何 KPI 被产出
    assert after.kpis == []


def test_refine_empty_filters_returns_full_scope(tmp_path: Path):
    store, sid = _published(tmp_path)
    snapshot = store.load_snapshot(sid)
    from app.schemas.bundle import AnalysisBundle
    from app.services.bundle_planner import refine_bundle
    source = AnalysisBundle.model_validate(store.read_bundle(sid))
    new_bundle = refine_bundle(source, snapshot, [])
    store.write_bundle(sid, new_bundle.model_dump(mode="json"))
    execute_bundle(sid, store, new_bundle)
    after = synthesize_dashboard(sid, store)

    assert after.scope.participating_rows == after.scope.snapshot_rows
    assert after.scope.filters == []


def test_chained_refine_strips_previous_scope_steps(tmp_path: Path):
    """从已筛选运行再次 refine/清除时，旧注入的 s_scope_* 步骤不得残留。"""
    store, sid = _published(tmp_path)
    snapshot = store.load_snapshot(sid)
    from app.schemas.bundle import AnalysisBundle
    from app.services.bundle_planner import refine_bundle

    first = AnalysisBundle.model_validate(store.read_bundle(sid))
    filtered = refine_bundle(first, snapshot, [
        {"column": "渠道", "values": ["线上商城"]}
    ])
    # 第二轮基于「已筛选 Bundle」清除筛选
    cleared = refine_bundle(filtered, snapshot, [])

    for view in cleared.analysis_views:
        step_ids = [s.step_id for s in view.plan.steps]
        assert not any(str(i).startswith("s_scope_") for i in step_ids)

    # 执行 + 合成恢复全量 KPI
    store.write_bundle(sid, cleared.model_dump(mode="json"))
    execute_bundle(sid, store, cleared)
    after = synthesize_dashboard(sid, store)
    assert after.scope.participating_rows == after.scope.snapshot_rows
    amount_kpi = next(k for k in after.kpis if k.label == "金额")
    assert abs(amount_kpi.value - float(snapshot["金额"].sum())) < 1e-6


# ----------------------------------------------------------------- API 层

def test_rows_endpoint_paging(api_store, tmp_path: Path):
    _, sid, _, _ = _ready_session("sales_orders", tmp_path)
    execution = _bundle_executed(sid)
    view_id = execution["views"][0]["view_id"]

    # 先发布，拿到 run_id
    r = client.post(f"/sessions/{sid}/dashboard")
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    resp = client.get(
        f"/sessions/{sid}/runs/{run_id}/views/{view_id}/rows",
        params={"page": 1, "page_size": 20},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["page"]["page"] == 1
    assert body["page"]["page_size"] == 20
    assert len(body["rows"]) <= 20
    assert body["page"]["total_rows"] >= body["page"]["total_pages"]


def test_runs_list_and_historical_dashboard(api_store, tmp_path: Path):
    _, sid = _published(tmp_path)
    resp = client.get(f"/sessions/{sid}/runs")
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert len(runs) >= 1
    published = [r for r in runs if r["has_dashboard"]]
    assert published

    run_id = published[0]["run_id"]
    got = client.get(f"/sessions/{sid}/runs/{run_id}/dashboard")
    assert got.status_code == 200
    assert got.json()["run_id"] == run_id


def test_refine_endpoint_recomputes_and_publishes(api_store, tmp_path: Path):
    _, sid, _, _ = _ready_session("sales_orders", tmp_path)
    _bundle_executed(sid)
    r = client.post(f"/sessions/{sid}/dashboard")
    assert r.status_code == 200, r.text
    f = r.json()["global_filters"][0]
    member = f["members"][0]

    resp = client.post(f"/sessions/{sid}/dashboard/refine", json={
        "filters": [{"column": f["column"], "values": [member]}],
    })
    assert resp.status_code == 200, resp.text
    artifact = resp.json()
    assert artifact["state"] in {"ready", "partial"}
    assert artifact["scope"]["participating_rows"] < artifact["scope"]["snapshot_rows"]

    # 所有可消费视图卡均带数据信封，浏览器无需接触 parquet
    for card in artifact["views"].values():
        if card["consumable"]:
            assert card["data"] is not None

    # current 已原子切换：GET 直接返回新版本（不是 stale）
    got = client.get(f"/sessions/{sid}/dashboard")
    assert got.status_code == 200
    assert got.json()["run_id"] == artifact["run_id"]
