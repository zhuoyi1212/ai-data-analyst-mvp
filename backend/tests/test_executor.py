"""TR-11.1/11.2/11.3：台账完整性、中文阻断、20 万行性能。"""
from __future__ import annotations

import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.common import AggFunc, ChartType, Confidence, FilterOperator, SemanticType, TimeGranularity
from app.schemas.dictionary import DataDictionary, FieldProfile
from app.schemas.ledger import Ledger
from app.schemas.plan import (
    AggregateParams,
    AnalysisPlan,
    FilterParams,
    FilterStep,
    GroupByParams,
    GroupByStep,
    PlanArtifact,
    TimeSeriesParams,
    TimeSeriesStep,
)
from app.services.executor import ExecutionGateError, execute
from app.services.planner import plan_hash
from app.services.storage import SessionStore


def _dict(columns) -> DataDictionary:
    type_map = {"日期": SemanticType.date, "区域": SemanticType.dimension,
                "品类": SemanticType.dimension, "金额": SemanticType.metric}
    return DataDictionary(session_id="x", complete=True, fields=[
        FieldProfile(name=c, physical_type="string", semantic_type=type_map[c],
                     confidence=Confidence.high, confirmed=True, confirmed_by_user=True)
        for c in columns])


def _session(tmp_path, df: pd.DataFrame, plan: AnalysisPlan, *, locked=True, tamper=False):
    store = SessionStore(tmp_path / "storage")
    sid = store.create_from_bytes("d.csv", df.to_csv(index=False).encode())["session_id"]
    store.write_artifact(sid, "dictionary",
                         {**_dict(df.columns).model_dump(mode="json"), "session_id": sid})
    snap_hash = store.save_snapshot(sid, df)
    store.write_artifact(sid, "quality",
                         {"complete": True, "snapshot_hash": snap_hash,
                          "snapshot_rows": len(df), "issues": [], "decisions": {}})
    artifact = PlanArtifact(
        question=plan.question, plan=plan,
        chart=ChartType.bar, locked=locked,
        plan_hash=(plan_hash(plan) if locked else None))
    if tamper:
        artifact.plan_hash = "0" * 16
    store.write_artifact(sid, "plan", artifact.model_dump(mode="json"))
    return store, sid


def _two_step_plan() -> AnalysisPlan:
    return AnalysisPlan(
        question="东区各品类销售额", data_scope="全量",
        steps=[
            FilterStep(step_id="f1", op="filter",
                       params=FilterParams(column="区域", operator=FilterOperator.eq, value="East"),
                       description="只看东区"),
            GroupByStep(step_id="g1", op="group_by",
                        params=GroupByParams(dimension="品类", metric="金额", func=AggFunc.sum),
                        description="按品类汇总金额", depends_on=["f1"]),
        ])


@pytest.fixture
def sales() -> pd.DataFrame:
    return pd.DataFrame({
        "区域": ["East", "East", "East", "West"],
        "品类": ["A", "A", "B", "A"],
        "金额": [100.0, 200.0, 50.0, 1000.0],
    })


def test_ledger_complete_and_values_match(sales, tmp_path):
    store, sid = _session(tmp_path, sales, _two_step_plan())
    ledger = execute(sid, store)
    assert isinstance(ledger, Ledger)
    assert ledger.snapshot_rows == 4
    assert ledger.participating_rows == 3  # 筛选后进入分组
    assert ledger.chart == ChartType.bar
    assert ledger.plan_hash and len(ledger.plan_hash) == 16
    # 快照哈希与 parquet 实体一致
    import hashlib

    h = hashlib.sha256((store.session_dir(sid) / "snapshot.parquet").read_bytes()).hexdigest()
    assert ledger.snapshot_hash == h
    # 逐步台账
    assert [s.op for s in ledger.steps] == ["filter", "group_by"]
    assert ledger.steps[0].summary == {"input_rows": 4, "output_rows": 3}
    # 最终数值手算一致
    rows = {r["品类"]: r["value"] for r in ledger.result_preview}
    assert rows == {"A": 300.0, "B": 50.0}
    assert ledger.result_rows_total == 2
    assert store.has_artifact(sid, "ledger")
    assert (store.session_dir(sid) / "result.parquet").exists()


def test_engine_error_blocks_without_ledger(tmp_path):
    df = pd.DataFrame({"区域": ["East", "West"], "品类": ["A", "B"]})  # 缺金额列
    plan = _two_step_plan()
    store, sid = _session(tmp_path, df, plan)
    from app.services.engine.ops import EngineError

    with pytest.raises(EngineError, match="不存在"):
        execute(sid, store)
    assert not store.has_artifact(sid, "ledger")  # 失败不写台账
    assert not (store.session_dir(sid) / "result.parquet").exists()


def test_unlocked_and_tampered_plan_blocked(sales, tmp_path):
    store, sid = _session(tmp_path, sales, _two_step_plan(), locked=False)
    with pytest.raises(ExecutionGateError, match="确认"):
        execute(sid, store)

    store2, sid2 = _session(tmp_path, sales, _two_step_plan(), tamper=True)
    with pytest.raises(ExecutionGateError, match="哈希"):
        execute(sid2, store2)


def test_execute_api_400_on_engine_error(tmp_path, monkeypatch):
    df = pd.DataFrame({"区域": ["East"], "品类": ["A"]})
    store, sid = _session(tmp_path, df, _two_step_plan())
    from app.routers import execute as exec_router

    monkeypatch.setattr(exec_router, "SessionStore", lambda: store)
    resp = TestClient(app).post(f"/sessions/{sid}/execute")
    assert resp.status_code == 400
    assert "不存在" in resp.json()["detail"]


def test_perf_200k_rows_groupby_and_timeseries(tmp_path):
    """TR-11.3：20 万行 group_by/time_series ≤ 5s。"""
    n = 200_000
    df = pd.DataFrame({
        "日期": pd.date_range("2024-01-01", periods=n, freq="h"),
        "区域": pd.Series(["East", "West", "North", "South"] * (n // 4)),
        "品类": pd.Series(["A", "B", "C"] * (n // 3 + 1))[:n],
        "金额": pd.Series(range(n), dtype="float64"),
    })
    plan = AnalysisPlan(
        question="性能：区域分组", data_scope="全量",
        steps=[GroupByStep(step_id="g1", op="group_by",
                           params=GroupByParams(dimension="区域", metric="金额", func=AggFunc.sum))])
    store, sid = _session(tmp_path, df, plan)
    t0 = time.perf_counter()
    ledger = execute(sid, store)
    group_ms = (time.perf_counter() - t0) * 1000
    assert ledger.result_rows_total == 4
    assert group_ms < 5000

    plan2 = AnalysisPlan(
        question="性能：月度趋势", data_scope="全量",
        steps=[TimeSeriesStep(step_id="t1", op="time_series",
                              params=TimeSeriesParams(date_column="日期",
                                                      granularity=TimeGranularity.month,
                                                      metric="金额", func=AggFunc.sum))])
    store2, sid2 = _session(tmp_path, df, plan2)
    t0 = time.perf_counter()
    ledger2 = execute(sid2, store2)
    ts_ms = (time.perf_counter() - t0) * 1000
    assert ledger2.result_rows_total >= 12
    assert ts_ms < 5000
