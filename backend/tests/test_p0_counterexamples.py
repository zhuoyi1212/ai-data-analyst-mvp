"""P0 可信度反例验收（执行清单 T01–T05 锁定的 10 个已复现反例）。

每个用例对应《Trae执行清单-分析产品升级.md》中的一条反例，修复前应失败、
修复后必须通过。数值只允许来自确定性引擎算子。
"""
from __future__ import annotations

import dataclasses
import io
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.schemas.bundle import (
    AnalysisBundle,
    AnalysisView,
    ViewStatus,
    ViewType,
)
from app.schemas.common import AggFunc, ComparePeriod, CorrMethod, TimeGranularity
from app.schemas.dictionary import DataDictionary, FieldProfile
from app.schemas.plan import (
    AnalysisPlan,
    FilterParams,
    FilterStep,
    GroupByParams,
    GroupByStep,
    PeriodCompareParams,
    PeriodCompareStep,
    CorrelationParams,
    CorrelationStep,
)
from app.schemas.common import Confidence, SemanticType, FilterOperator
from app.services.bundle_executor import execute_bundle
from app.services.bundle_planner import build_bundle
from app.services.dashboard_synthesizer import synthesize_dashboard
from app.services.engine.ops import run_step
from app.services.metric_spec import infer_metric_spec
from app.services.profiler import confirm_fields, generate_dictionary
from app.services.quality_actions import apply_decisions
from app.services.quality_checker import run_checks
from app.services.storage import SessionStore
from test_bundle_planner import _ready_session

client = TestClient(app)


@pytest.fixture
def api_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """让路由层 SessionStore() 与 _ready_session 的 tmp_path/store 指向同一目录。"""
    test_settings = dataclasses.replace(settings, storage_dir=tmp_path / "store")
    import app.services.storage as storage_mod
    monkeypatch.setattr(storage_mod, "settings", test_settings)
    return test_settings.storage_dir


# ----------------------------------------------------------- 反例 1：运行版本混用

def test_counterexample_1_stale_downstream_not_consumed(api_store, tmp_path: Path):
    """重新生成 Bundle 后，旧 Dashboard 不得作为当前产物消费（KPI 张冠李戴被阻断）。"""
    store, sid, dictionary, snapshot = _ready_session("sales_orders", tmp_path)

    # 第一次完整运行并发布
    r = client.post(f"/sessions/{sid}/analysis-bundle")
    assert r.status_code == 200
    first_bundle = r.json()
    assert client.post(f"/sessions/{sid}/analysis-bundle/execute").status_code == 200
    assert client.post(f"/sessions/{sid}/dashboard").status_code == 200
    got = client.get(f"/sessions/{sid}/dashboard")
    assert got.status_code == 200
    first_run_id = got.json()["run_id"]

    # 重新生成 Bundle（新 run）：当前 Dashboard 立即变为陈旧
    r = client.post(f"/sessions/{sid}/analysis-bundle")
    assert r.status_code == 200
    assert r.json()["bundle_id"] != first_bundle["bundle_id"]
    stale = client.get(f"/sessions/{sid}/dashboard")
    assert stale.status_code == 409  # 旧产物可读但明确不是当前版本
    # 未执行的新 run 不允许合成
    assert client.post(f"/sessions/{sid}/dashboard").status_code == 409
    # 执行 + 合成后发布新版本
    assert client.post(f"/sessions/{sid}/analysis-bundle/execute").status_code == 200
    assert client.post(f"/sessions/{sid}/dashboard").status_code == 200
    got2 = client.get(f"/sessions/{sid}/dashboard")
    assert got2.status_code == 200
    assert got2.json()["run_id"] != first_run_id


def test_kpi_label_matches_its_own_metric_value(tmp_path: Path):
    """每个 KPI 的数值必须来自其同名指标（Sales 卡显示 ΣSales，不得错挂 Profit）。"""
    store, sid, dictionary, snapshot = _ready_session("sales_orders", tmp_path)
    bundle = build_bundle(dictionary, snapshot, snapshot_rows=len(snapshot), title="x")
    store.write_bundle(sid, bundle.model_dump(mode="json"))
    execute_bundle(sid, store, bundle)
    artifact = synthesize_dashboard(sid, store)
    for kpi in artifact.kpis:
        if kpi.unit == "%":
            continue  # 派生比率 KPI
        assert kpi.label in snapshot.columns
        expected = float(pd.to_numeric(snapshot[kpi.label], errors="coerce").sum())
        assert kpi.value == pytest.approx(expected)


def test_snapshot_change_invalidates_run(tmp_path: Path):
    """快照被替换（哈希变化）后，合成必须 409，而不是用旧执行结果拼新数据。"""
    store, sid, dictionary, snapshot = _ready_session("sales_orders", tmp_path)
    bundle = build_bundle(dictionary, snapshot, snapshot_rows=len(snapshot), title="x")
    store.write_bundle(sid, bundle.model_dump(mode="json"))
    execute_bundle(sid, store, bundle)

    # 直接重写快照文件（模拟质量处理被重做）
    store.save_snapshot(sid, snapshot.head(10))
    with pytest.raises(Exception):  # noqa: B017 — StaleRunError/ValueError/HTTP 皆可
        synthesize_dashboard(sid, store)


def test_repeated_synthesis_is_idempotent_and_probe_budget_persists(api_store, tmp_path: Path):
    store, sid, _, _ = _ready_session("sales_orders", tmp_path)
    store2 = store
    r = client.post(f"/sessions/{sid}/analysis-bundle")
    assert r.status_code == 200
    client.post(f"/sessions/{sid}/analysis-bundle/execute")
    client.post(f"/sessions/{sid}/dashboard")
    a1 = client.get(f"/sessions/{sid}/dashboard").json()
    client.post(f"/sessions/{sid}/dashboard")  # 重复合成
    a2 = client.get(f"/sessions/{sid}/dashboard").json()
    assert a1["run_id"] == a2["run_id"]
    probe_root = store2.active_run_dir(sid) / "probes"
    probes = [p for p in probe_root.iterdir() if p.is_dir()] if probe_root.exists() else []
    assert len(probes) <= 3  # 不因重复合成重置预算


# ----------------------------------------------------------- 反例 2：零行假成功

def test_counterexample_2_zero_rows_not_consumed_as_zero_kpi(tmp_path: Path):
    """筛选零行：status=success 但有效性=no_data，KPI/图表/Finding 都不得消费。"""
    store, sid, dictionary, snapshot = _ready_session("sales_orders", tmp_path)
    bundle = build_bundle(dictionary, snapshot, snapshot_rows=len(snapshot), title="x")

    dim = bundle.primary_dimensions[0]
    missing_member = "__不存在的成员__"
    metric = bundle.primary_metrics[0]
    empty = AnalysisView(
        view_id="view_empty", title="零行视角", type=ViewType.overview,
        question="空筛选的指标是多少？", chart=bundle.analysis_views[0].chart,
        metric_fields=[metric], dimension_fields=[dim],
        plan=AnalysisPlan(
            question="空筛选的指标是多少？", data_scope="无",
            steps=[
                FilterStep(step_id="f", op="filter",
                           params=FilterParams(column=dim, operator=FilterOperator.eq,
                                               value=missing_member)),
            ],
            expected_shape="零行",
        ),
    )
    poisoned = AnalysisBundle(
        bundle_id=bundle.bundle_id, title=bundle.title,
        primary_metrics=bundle.primary_metrics,
        primary_dimensions=bundle.primary_dimensions,
        analysis_views=bundle.analysis_views + [empty],
    )
    # 注入视角必须先随 Bundle 落盘（建立 run manifest），再执行
    store.write_bundle(sid, poisoned.model_dump(mode="json"))
    summary = execute_bundle(sid, store, poisoned)
    r = next(v for v in summary.views if v.view_id == "view_empty")
    assert r.status == ViewStatus.success          # 引擎确实成功执行
    assert r.consumable is False                   # 但结果不可消费
    assert any(c.level == "no_data" for c in r.checks)

    artifact = synthesize_dashboard(sid, store)
    slotted = {vid for s in artifact.sections for vid in s.view_ids}
    assert "view_empty" not in slotted
    assert all(k.value != 0 for k in artifact.kpis)  # 不允许伪造 0 KPI


# ----------------------------------------------------------- 反例 3：ChartSpec 字段错配

def test_counterexample_3_chart_spec_fields_exist_in_output(tmp_path: Path):
    """time_series 输出列为 {日期列, value}，ChartSpec 的 x/y 必须命中输出 schema。"""
    store, sid, dictionary, snapshot = _ready_session("sales_orders", tmp_path)
    bundle = build_bundle(dictionary, snapshot, snapshot_rows=len(snapshot), title="x")
    store.write_bundle(sid, bundle.model_dump(mode="json"))
    execute_bundle(sid, store, bundle)
    synthesize_dashboard(sid, store)

    from app.services.dashboard_synthesizer import build_chart_spec
    for v in bundle.analysis_views:
        spec = build_chart_spec(v)
        if spec is None:
            continue
        df = pd.read_parquet(store.view_dir(sid, v.view_id) / "result.parquet")
        cols = set(df.columns)
        if spec.x_field:
            assert spec.x_field in cols, f"{v.view_id} x_field {spec.x_field} 不在输出列"
        for yf in spec.y_fields:
            assert yf in cols, f"{v.view_id} y_field {yf} 不在输出列 {cols}"


# ----------------------------------------------------------- 反例 4：r=0 强占视图

def test_counterexample_4_zero_correlation_not_selected():
    """100 行有效数据但 Pearson r=0：relationship 只能作为 computation 隐藏证据，不占默认视图。"""
    dates = pd.date_range("2024-01-01", periods=100, freq="D")
    # [0,1,1,0] 在每个长度 4 的对称块内与线性 X 严格正交，100 行 Pearson r=0
    y_pattern = [0, 1, 1, 0]
    df = pd.DataFrame({
        "日期": dates,
        "区域": (["华东", "华南", "华北", "华北"] * 25),
        "X": list(range(100)),
        "Y": [y_pattern[i % 4] for i in range(100)],
    })
    assert abs(df["X"].corr(df["Y"].astype(float))) < 1e-12
    dictionary = DataDictionary(session_id="s", fields=[
        FieldProfile(name="日期", physical_type="datetime", semantic_type=SemanticType.date,
                     meaning="日期", confidence=Confidence.high, confirmed=True,
                     cardinality=100, is_date=True),
        FieldProfile(name="区域", physical_type="string", semantic_type=SemanticType.dimension,
                     meaning="区域", confidence=Confidence.high, confirmed=True, cardinality=4),
        FieldProfile(name="X", physical_type="integer", semantic_type=SemanticType.metric,
                     meaning="X", confidence=Confidence.high, confirmed=True,
                     cardinality=100, is_metric=True),
        FieldProfile(name="Y", physical_type="integer", semantic_type=SemanticType.metric,
                     meaning="Y", confidence=Confidence.high, confirmed=True,
                     cardinality=2, is_metric=True),
    ], complete=True)
    bundle = build_bundle(dictionary, df, snapshot_rows=100, title="zero-r")
    rel_views = [v for v in bundle.analysis_views if v.type is ViewType.relationship]
    # T07：r=0 不占默认视图——存在时也只能是 computation
    assert all(v.role == "computation" for v in rel_views)


# ----------------------------------------------------------- 反例 5/6/7：时间口径

def _period_step(df: pd.DataFrame, date_col: str, period: ComparePeriod,
                 metric: str = "Sales") -> dict:
    step = PeriodCompareStep(
        step_id="cmp", op="period_compare",
        params=PeriodCompareParams(
            date_column=date_col, period=period, metric=metric, func=AggFunc.sum
        ),
    )
    return run_step(df, step).summary


def test_counterexample_5_partial_month_compares_equal_length_window():
    """1 月完整 31 天、2 月仅 10 天，每日均为 100：不得得出 -67.7%。"""
    jan = pd.date_range("2024-01-01", "2024-01-31", freq="D")
    feb = pd.date_range("2024-02-01", "2024-02-10", freq="D")
    df = pd.DataFrame({
        "Date": jan.append(feb),
        "Sales": [100.0] * (len(jan) + len(feb)),
    })
    s = _period_step(df, "Date", ComparePeriod.mom)
    assert s["completeness"] == "partial"
    assert s["growth_pct"] is None or abs(s["growth_pct"]) < 1e-9
    assert s["delta"] == pytest.approx(0.0, abs=1e-6)  # 等长 MTD：2/1–2/10 vs 1/1–1/10


def test_counterexample_6_negative_base_uses_delta_and_status():
    """Profit -100 → +100：引擎与 Dashboard 都不得输出 ±200% 增长率。"""
    df = pd.DataFrame({
        "Date": pd.to_datetime(["2024-01-15", "2024-02-15"]),
        "Profit": [-100.0, 100.0],
    })
    s = _period_step(df, "Date", ComparePeriod.mom, metric="Profit")
    assert s["growth_pct"] is None
    assert s["delta"] == pytest.approx(200.0)
    assert "扭亏" in s["status"]


def test_counterexample_7_yoy_aligns_same_period_not_annual_cumulative():
    """2023-01=100、2023-02=200、2024-01=150：1 月同比应为 +50%，不是年度累计 -50%。"""
    df = pd.DataFrame({
        "Date": pd.to_datetime([
            "2023-01-10", "2023-01-20", "2023-02-10", "2023-02-20",
            "2024-01-10", "2024-01-20",
        ]),
        "Sales": [50.0, 50.0, 100.0, 100.0, 75.0, 75.0],
    })
    s = _period_step(df, "Date", ComparePeriod.yoy)
    assert s["growth_pct"] == pytest.approx(50.0)
    assert s["current_value"] == pytest.approx(150.0)
    assert s["previous_value"] == pytest.approx(100.0)


# ----------------------------------------------------------- 反例 8：MetricSpec 口径

@pytest.mark.parametrize("column,expected_agg,additive", [
    ("ending_stock", "snapshot_last", False),
    ("satisfaction", "mean", False),
    ("conversion_rate", "mean", False),
    ("profit_margin", "mean", False),
    ("sales_qty", "sum", True),
])
def test_counterexample_8_metric_spec_default_aggregation(column, expected_agg, additive):
    fp = FieldProfile(
        name=column, physical_type="float", semantic_type=SemanticType.metric,
        meaning=column, confidence=Confidence.high, confirmed=True,
        cardinality=100, is_metric=True,
    )
    spec = infer_metric_spec(fp)
    assert spec is not None
    assert spec.aggregation == expected_agg
    assert spec.additive is additive


def test_non_additive_metric_does_not_get_share_or_pie():
    """满意度等不可加指标：不生成 share 视角，也不分配饼图。"""
    dates = pd.date_range("2024-01-01", periods=60, freq="D")
    df = pd.DataFrame({
        "日期": dates,
        "区域": (["华东", "华南", "华北"] * 20),
        "satisfaction": [4.0 + (i % 10) / 10 for i in range(60)],
    })
    dictionary = DataDictionary(session_id="s", fields=[
        FieldProfile(name="日期", physical_type="datetime", semantic_type=SemanticType.date,
                     meaning="日期", confidence=Confidence.high, confirmed=True,
                     cardinality=60, is_date=True),
        FieldProfile(name="区域", physical_type="string", semantic_type=SemanticType.dimension,
                     meaning="区域", confidence=Confidence.high, confirmed=True, cardinality=3),
        FieldProfile(name="satisfaction", physical_type="float",
                     semantic_type=SemanticType.metric, meaning="满意度",
                     confidence=Confidence.high, confirmed=True, cardinality=10, is_metric=True),
    ], complete=True)
    bundle = build_bundle(dictionary, df, snapshot_rows=60, title="rate-only")
    assert not any(v.type is ViewType.breakdown for v in bundle.analysis_views)
    assert all(v.chart.value != "pie" for v in bundle.analysis_views)
    ov = next(v for v in bundle.analysis_views if v.type is ViewType.overview)
    assert ov.plan.steps[-1].params.func == AggFunc.mean


# ----------------------------------------------------------- 反例 9：清洗吃掉业务信号

def test_counterexample_9_legit_negative_profit_is_kept():
    """Profit 0…99 + 合法 -1000：不得建议排除负值；采纳安全建议后合计仍为 3950。"""
    profit = list(range(100)) + [-1000]
    df = pd.DataFrame({
        "Profit": profit,
        "Region": ["华东"] * 101,
    })
    dictionary = DataDictionary(session_id="s", fields=[
        FieldProfile(name="Profit", physical_type="integer",
                     semantic_type=SemanticType.metric, meaning="利润",
                     confidence=Confidence.high, confirmed=True,
                     cardinality=101, is_metric=True),
        FieldProfile(name="Region", physical_type="string",
                     semantic_type=SemanticType.dimension, meaning="区域",
                     confidence=Confidence.high, confirmed=True, cardinality=1),
    ], complete=True)
    issues = run_checks(df, dictionary)
    outlier_issues = [i for i in issues if i.type.value == "outlier" and i.column == "Profit"]
    assert all(i.suggested_action != "exclude" for i in outlier_issues)
    missing = [i for i in issues if i.type.value == "missing"]
    assert all(i.suggested_action == "keep" for i in missing)


def test_counterexample_9_safe_decisions_preserve_total(tmp_path: Path):
    rows = [{"Profit": float(v), "Region": "华东"} for v in list(range(100)) + [-1000]]
    buf = io.BytesIO()
    pd.DataFrame(rows).to_csv(buf, index=False)
    store = SessionStore(tmp_path / "store")
    sid = store.create_from_bytes("p.csv", buf.getvalue())["session_id"]
    generate_dictionary(sid, store)
    report = run_quality_checks_safe(sid, store)
    # 安全策略：只转格式；缺失保留；离群保留；重复保留——绝不自动删行/填均值
    safe = {}
    for i in report.issues:
        if i.type.value == "format":
            safe[i.issue_id] = {"action": "convert"}
        else:
            safe[i.issue_id] = {"action": "keep"}
    report2 = apply_decisions(sid, safe, store)
    snap = store.load_snapshot(sid)
    assert float(snap["Profit"].sum()) == pytest.approx(3950.0)
    assert report2.impact["rows_after"] == 101


def run_quality_checks_safe(sid, store):
    from app.services.quality_checker import run_quality_checks
    return run_quality_checks(sid, store)


# ----------------------------------------------------------- 反例 10：Spearman 依赖

def test_counterexample_10_spearman_runs_in_locked_env():
    """锁文件全新环境中 Spearman 必须可执行（scipy 依赖齐备或引擎自算秩相关）。"""
    df = pd.DataFrame({"X": [1.0, 2, 3, 4, 5, 6, 7, 8],
                       "Y": [2.0, 1, 4, 3, 6, 5, 8, 7]})
    step = CorrelationStep(
        step_id="corr", op="correlation",
        params=CorrelationParams(column_x="X", column_y="Y", method=CorrMethod.spearman),
    )
    result = run_step(df, step)
    assert result.summary["n"] == 8
    assert -1 <= result.summary["coefficient"] <= 1
