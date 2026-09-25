"""TR-12.1/12.2：五类校验场景判定与 fail/warn 阻断行为。"""
from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.common import (
    AggFunc,
    ChartType,
    CheckLevel,
    Confidence,
    FilterOperator,
    SemanticType,
    TimeGranularity,
)
from app.schemas.dictionary import DataDictionary, FieldProfile
from app.schemas.plan import (
    AggregateParams,
    FilterParams,
    FilterStep,
    GroupByParams,
    GroupByStep,
    PlanArtifact,
    ShareParams,
    ShareStep,
    AnalysisPlan,
    TimeSeriesParams,
    TimeSeriesStep,
)
from app.services.executor import execute
from app.services.planner import plan_hash
from app.services.storage import SessionStore
from app.services.validator import (
    ValidationGateError,
    acknowledge,
    ensure_consumable,
    run_validation,
)

_TYPE = {"日期": SemanticType.date, "区域": SemanticType.dimension,
         "品类": SemanticType.dimension, "金额": SemanticType.metric}


def _dict(columns) -> DataDictionary:
    return DataDictionary(session_id="x", complete=True, fields=[
        FieldProfile(name=c, physical_type="string", semantic_type=_TYPE[c],
                     confidence=Confidence.high, confirmed=True, confirmed_by_user=True)
        for c in columns])


def _session(tmp_path, df, plan):
    store = SessionStore(tmp_path / "storage")
    sid = store.create_from_bytes("d.csv", df.to_csv(index=False).encode())["session_id"]
    store.write_artifact(sid, "dictionary",
                         {**_dict(df.columns).model_dump(mode="json"), "session_id": sid})
    snap_hash = store.save_snapshot(sid, df)
    store.write_artifact(sid, "quality",
                         {"complete": True, "snapshot_hash": snap_hash,
                          "snapshot_rows": len(df), "issues": [], "decisions": {}})
    artifact = PlanArtifact(question=plan.question, plan=plan, chart=ChartType.bar,
                            locked=True, plan_hash=plan_hash(plan))
    store.write_artifact(sid, "plan", artifact.model_dump(mode="json"))
    return store, sid


def _levels(report):
    return {i.code: i.level for i in report.items}


def test_normal_plan_all_pass(tmp_path):
    df = pd.DataFrame({"区域": ["East", "East", "West"], "金额": [100.0, 200.0, 300.0]})
    plan = AnalysisPlan(question="区域销售额", steps=[
        GroupByStep(step_id="g1", op="group_by",
                    params=GroupByParams(dimension="区域", metric="金额", func=AggFunc.sum))])
    store, sid = _session(tmp_path, df, plan)
    execute(sid, store)
    report = run_validation(sid, store)
    assert report.overall == "pass"
    assert set(_levels(report).values()) == {CheckLevel.passed}
    ensure_consumable(sid, store)  # 不抛异常


def test_filter_removal_explained(tmp_path):
    df = pd.DataFrame({"区域": ["East", "West", "West", "West"], "金额": [10.0, 1.0, 2.0, 3.0]})
    plan = AnalysisPlan(question="东区销售额", steps=[
        FilterStep(step_id="f1", op="filter",
                   params=FilterParams(column="区域", operator=FilterOperator.eq, value="East")),
        GroupByStep(step_id="g1", op="group_by",
                    params=GroupByParams(dimension="区域", metric="金额", func=AggFunc.sum),
                    depends_on=["f1"]),
    ])
    store, sid = _session(tmp_path, df, plan)
    execute(sid, store)
    report = run_validation(sid, store)
    cov = next(i for i in report.items if i.code == "coverage")
    assert cov.level == CheckLevel.passed
    assert cov.numbers["removed_by_filters"] == 3
    assert "筛选" in cov.detail


def test_share_cross_check(tmp_path):
    df = pd.DataFrame({"区域": ["East", "West"], "金额": [75.0, 25.0]})
    plan = AnalysisPlan(question="区域占比", steps=[
        ShareStep(step_id="s1", op="share",
                  params=ShareParams(dimension="区域", metric="金额", func=AggFunc.sum))])
    store, sid = _session(tmp_path, df, plan)
    execute(sid, store)
    report = run_validation(sid, store)
    cross = next(i for i in report.items if i.code == "cross_check")
    assert cross.level == CheckLevel.passed
    assert cross.numbers["share_sum"] == pytest.approx(1.0)


def test_tampered_result_fails_and_blocks(tmp_path):
    df = pd.DataFrame({"区域": ["East", "West"], "金额": [100.0, 300.0]})
    plan = AnalysisPlan(question="区域销售额", steps=[
        GroupByStep(step_id="g1", op="group_by",
                    params=GroupByParams(dimension="区域", metric="金额", func=AggFunc.sum))])
    store, sid = _session(tmp_path, df, plan)
    execute(sid, store)
    # 篡改全量结果
    tampered = pd.read_parquet(store.session_dir(sid) / "result.parquet")
    tampered.loc[0, "value"] = 99999.0
    tampered.to_parquet(store.session_dir(sid) / "result.parquet", index=False)
    report = run_validation(sid, store)
    assert report.overall == "fail"
    cross = next(i for i in report.items if i.code == "cross_check")
    assert cross.level == CheckLevel.failed
    with pytest.raises(ValidationGateError):
        ensure_consumable(sid, store)
    with pytest.raises(ValidationGateError):
        acknowledge(sid, store)


def test_plan_tampering_after_execute_fails(tmp_path):
    df = pd.DataFrame({"区域": ["East", "West"], "金额": [100.0, 300.0]})
    plan = AnalysisPlan(question="区域销售额", steps=[
        GroupByStep(step_id="g1", op="group_by",
                    params=GroupByParams(dimension="区域", metric="金额", func=AggFunc.sum))])
    store, sid = _session(tmp_path, df, plan)
    execute(sid, store)
    # 篡改已确认方案文件（哈希不一致）
    import json

    plan_path = store.session_dir(sid) / "plan.json"
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["plan_hash"] = "deadbeefdeadbeef"
    plan_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    report = run_validation(sid, store)
    item = next(i for i in report.items if i.code == "plan_consistency")
    assert item.level == CheckLevel.failed
    assert report.overall == "fail"


def test_time_gap_and_nulls_warn_then_acknowledge(tmp_path):
    # 1 月与 3 月有数据，2 月缺失；金额含一个空值
    df = pd.DataFrame({
        "日期": pd.to_datetime(["2024-01-10", "2024-01-20", "2024-03-05", "2024-03-15"]),
        "金额": [100.0, 50.0, 200.0, None],
    })
    plan = AnalysisPlan(question="月度趋势", steps=[
        TimeSeriesStep(step_id="t1", op="time_series",
                       params=TimeSeriesParams(date_column="日期",
                                               granularity=TimeGranularity.month,
                                               metric="金额", func=AggFunc.sum))])
    store, sid = _session(tmp_path, df, plan)
    execute(sid, store)
    report = run_validation(sid, store)
    assert report.overall == "warn"
    shape = next(i for i in report.items if i.code == "shape")
    assert shape.level == CheckLevel.warning
    assert shape.numbers["nan_by_column"]["value"] == 1  # 2 月缺口
    nulls = next(i for i in report.items if i.code == "null_handling")
    assert nulls.level == CheckLevel.warning
    assert nulls.numbers["null_metric_excluded"] == 1
    # warn 未知悉前阻断
    with pytest.raises(ValidationGateError):
        ensure_consumable(sid, store)
    acknowledge(sid, store)
    ensure_consumable(sid, store)


def test_fail_acknowledge_api_409(tmp_path, monkeypatch):
    df = pd.DataFrame({"区域": ["East", "West"], "金额": [100.0, 300.0]})
    plan = AnalysisPlan(question="区域销售额", steps=[
        GroupByStep(step_id="g1", op="group_by",
                    params=GroupByParams(dimension="区域", metric="金额", func=AggFunc.sum))])
    store, sid = _session(tmp_path, df, plan)
    execute(sid, store)
    tampered = pd.read_parquet(store.session_dir(sid) / "result.parquet")
    tampered.loc[0, "value"] = 1.0
    tampered.to_parquet(store.session_dir(sid) / "result.parquet", index=False)
    run_validation(sid, store)

    from app.routers import validate as validate_router

    monkeypatch.setattr(validate_router, "SessionStore", lambda: store)
    client = TestClient(app)
    resp = client.post(f"/sessions/{sid}/validate/acknowledge")
    assert resp.status_code == 409
