"""重构 Phase 1：BundleExecutor 测试——批量成功、视图级隔离落盘、partial success。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas.bundle import (
    AnalysisBundle,
    AnalysisView,
    ViewStatus,
    ViewType,
)
from app.services.bundle_executor import execute_bundle
from app.services.bundle_planner import build_bundle
from test_bundle_planner import _ready_session


@pytest.mark.parametrize("name", ["sales_orders", "operations_daily",
                                  "marketing_campaigns", "inventory_movements",
                                  "customer_tickets"])
def test_all_views_execute_successfully_with_ledger(name: str, tmp_path: Path):
    store, sid, dictionary, snapshot = _ready_session(name, tmp_path)
    bundle = build_bundle(dictionary, snapshot, snapshot_rows=len(snapshot), title=name)
    summary = execute_bundle(sid, store, bundle)

    assert summary.total == len(bundle.analysis_views)
    assert summary.failed == 0
    assert summary.succeeded == summary.total

    # 每个成功 View：结果/台账/校验三件套独立落盘，且数值非空
    for view, result in zip(bundle.analysis_views, summary.views):
        assert result.status == ViewStatus.success
        assert result.result_rows_total > 0
        assert result.steps, f"{name}/{view.view_id} 缺少步骤记录"
        vdir = store.bundle_dir(sid) / "views" / view.view_id
        assert (vdir / "result.parquet").exists()
        ledger = json.loads((vdir / "ledger.json").read_text(encoding="utf-8"))
        assert ledger["question"] == view.question
        assert ledger["snapshot_rows"] == len(snapshot)
        assert ledger["steps"], "台账必须保留完整计算过程"
        checks = json.loads((vdir / "validation.json").read_text(encoding="utf-8"))
        codes = {c["code"] for c in checks["checks"]}
        assert codes == {"coverage", "shape", "null_handling"}
        assert not any(c["level"] == "fail" for c in checks["checks"])

    # 汇总落盘
    persisted = json.loads(
        (store.bundle_dir(sid) / "execution.json").read_text(encoding="utf-8"))
    assert persisted["succeeded"] == summary.total

    # 视图级隔离：旧单 plan 槽位未被触碰
    assert not store.has_artifact(sid, "ledger")


def test_partial_success_isolates_bad_view(tmp_path: Path):
    """单个 View 引擎失败必须被隔离：其余 View 成功、失败 View 有中文原因。"""
    store, sid, dictionary, snapshot = _ready_session("sales_orders", tmp_path)
    bundle = build_bundle(dictionary, snapshot, snapshot_rows=len(snapshot), title="x")

    # 注入一个对不存在列聚合的坏 View（构造时合法，执行时引擎拒绝）
    bad = AnalysisView.model_validate({
        "view_id": "view_99",
        "title": "坏视角",
        "type": "overview",
        "question": "不存在的列总和是多少？",
        "chart": "metric",
        "priority": 99,
        "metric_fields": ["不存在列"],
        "dimension_fields": [],
        "source": "test",
        "selection_reason": "注入失败",
        "plan": {
            "question": "不存在的列总和是多少？",
            "data_scope": "质量处理后的全部数据",
            "steps": [{
                "step_id": "agg", "op": "aggregate",
                "params": {"column": "不存在列", "func": "sum"},
                "description": "坏聚合",
            }],
            "expected_shape": "指标卡",
            "chart_hint": "metric",
        },
    })
    poisoned = AnalysisBundle(
        bundle_id=bundle.bundle_id, title=bundle.title,
        primary_metrics=bundle.primary_metrics,
        primary_dimensions=bundle.primary_dimensions,
        analysis_views=bundle.analysis_views + [bad],
    )

    summary = execute_bundle(sid, store, poisoned)
    assert summary.total == len(poisoned.analysis_views)
    assert summary.failed == 1
    assert summary.succeeded == len(bundle.analysis_views)

    failed = next(v for v in summary.views if v.view_id == "view_99")
    assert failed.status == ViewStatus.failed
    assert failed.reason and "不存在列" in failed.reason
    assert failed.steps == []

    # 坏 View 不产生任何落盘产物
    bad_dir = store.bundle_dir(sid) / "views" / "view_99"
    assert not (bad_dir / "ledger.json").exists()
    assert not (bad_dir / "result.parquet").exists()

    # 其余 View 全部成功
    assert all(
        v.status == ViewStatus.success
        for v in summary.views if v.view_id != "view_99"
    )
