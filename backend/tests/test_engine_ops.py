"""TR-10.1/10.2/10.3：10 算子手算黄金值、图表映射、引擎零 LLM 依赖。"""
from __future__ import annotations

import pathlib

import pandas as pd
import pytest

from app.schemas.common import (
    AggFunc,
    ChartType,
    ComparePeriod,
    CorrMethod,
    FilterOperator,
    OutlierMethod,
    TimeGranularity,
)
from app.schemas.plan import (
    AggregateParams,
    AggregateStep,
    CompareGroupsParams,
    CompareGroupsStep,
    CorrelationParams,
    CorrelationStep,
    FilterParams,
    FilterStep,
    GroupByParams,
    GroupByStep,
    OutlierFlagParams,
    OutlierFlagStep,
    PeriodCompareParams,
    PeriodCompareStep,
    ShareParams,
    ShareStep,
    TimeSeriesParams,
    TimeSeriesStep,
    TopNParams,
    TopNStep,
)
from app.services.engine import ops
from app.services.engine.catalog import CHART_BY_OP, OP_META
from app.services.engine.ops import EngineError, run_step


@pytest.fixture
def sales() -> pd.DataFrame:
    return pd.DataFrame({
        "日期": pd.to_datetime(
            ["2024-01-05", "2024-01-20", "2024-02-03", "2024-02-18",
             "2024-03-02", "2024-03-25"]),
        "区域": ["East", "West", "East", "West", "East", "West"],
        "金额": [100.0, 50.0, 200.0, 80.0, 300.0, 70.0],
        "数量": [10.0, 5.0, 20.0, 8.0, 30.0, 7.0],
    })


def _step(model, params_model, **kwargs):
    op = kwargs.pop("op_value")
    return model(step_id="s1", op=op, params=params_model(**kwargs))


def test_filter_then_aggregate(sales):
    f = run_step(sales, FilterStep(
        step_id="f1", op="filter",
        params=FilterParams(column="区域", operator=FilterOperator.eq, value="East")))
    assert len(f.df) == 3
    agg = run_step(f.df, AggregateStep(
        step_id="a1", op="aggregate",
        params=AggregateParams(column="金额", func=AggFunc.sum)))
    assert agg.df["value"].iloc[0] == 600.0
    assert agg.summary["value"] == 600.0


def test_aggregate_funcs(sales):
    a = run_step(sales, AggregateStep(
        step_id="s", op="aggregate",
        params=AggregateParams(column="金额", func=AggFunc.sum)))
    assert a.df["value"].iloc[0] == 800.0
    m = run_step(sales, AggregateStep(
        step_id="s", op="aggregate",
        params=AggregateParams(column="金额", func=AggFunc.mean)))
    assert m.df["value"].iloc[0] == pytest.approx(800 / 6)
    c = run_step(sales, AggregateStep(
        step_id="s", op="aggregate",
        params=AggregateParams(column=None, func=AggFunc.count)))
    assert c.df["value"].iloc[0] == 6
    d = run_step(sales, AggregateStep(
        step_id="s", op="aggregate",
        params=AggregateParams(column="区域", func=AggFunc.count_distinct)))
    assert d.df["value"].iloc[0] == 2


def test_group_by(sales):
    r = run_step(sales, GroupByStep(
        step_id="s", op="group_by",
        params=GroupByParams(dimension="区域", metric="金额", func=AggFunc.sum)))
    assert r.df["区域"].tolist() == ["East", "West"]
    assert r.df["value"].tolist() == [600.0, 200.0]


def test_share_sums_to_one(sales):
    r = run_step(sales, ShareStep(
        step_id="s", op="share",
        params=ShareParams(dimension="区域", metric="金额", func=AggFunc.sum)))
    got = dict(zip(r.df["区域"], r.df["share"]))
    assert got["East"] == pytest.approx(0.75)
    assert got["West"] == pytest.approx(0.25)
    assert r.summary["share_sum"] == pytest.approx(1.0)


def test_top_n(sales):
    r = run_step(sales, TopNStep(
        step_id="s", op="top_n",
        params=TopNParams(dimension="区域", metric="金额", func=AggFunc.sum, n=1)))
    assert r.df["区域"].tolist() == ["East"]
    assert r.df["value"].tolist() == [600.0]


def test_time_series_month(sales):
    r = run_step(sales, TimeSeriesStep(
        step_id="s", op="time_series",
        params=TimeSeriesParams(date_column="日期", granularity=TimeGranularity.month,
                                metric="金额", func=AggFunc.sum)))
    assert r.df["value"].tolist() == [150.0, 280.0, 370.0]
    assert r.df["日期"].iloc[0] == pd.Timestamp("2024-01-01")


def test_period_compare_growth(sales):
    r = run_step(sales, PeriodCompareStep(
        step_id="s", op="period_compare",
        params=PeriodCompareParams(date_column="日期", period=ComparePeriod.mom,
                                   metric="金额", func=AggFunc.sum)))
    assert r.summary["current_value"] == 370.0
    assert r.summary["previous_value"] == 280.0
    assert r.summary["growth_pct"] == pytest.approx((370 - 280) / 280 * 100)
    assert r.df["period"].tolist() == ["2024-03", "2024-02"]


def test_period_compare_missing_prev_blocked():
    df = pd.DataFrame({"日期": pd.to_datetime(["2024-03-01", "2024-03-10"]),
                       "金额": [1.0, 2.0]})
    with pytest.raises(EngineError, match="上一周期"):
        run_step(df, PeriodCompareStep(
            step_id="s", op="period_compare",
            params=PeriodCompareParams(date_column="日期", period=ComparePeriod.mom,
                                       metric="金额", func=AggFunc.sum)))


def test_compare_groups(sales):
    r = run_step(sales, CompareGroupsStep(
        step_id="s", op="compare_groups",
        params=CompareGroupsParams(dimension="区域", members=["West", "East"],
                                   metric="金额", func=AggFunc.sum)))
    assert r.df["区域"].tolist() == ["West", "East"]  # 按指定成员顺序
    assert r.df["value"].tolist() == [200.0, 600.0]
    with pytest.raises(EngineError, match="不存在"):
        run_step(sales, CompareGroupsStep(
            step_id="s", op="compare_groups",
            params=CompareGroupsParams(dimension="区域", members=["East", "Mars"],
                                       metric="金额", func=AggFunc.sum)))


def test_correlation_sign():
    df = pd.DataFrame({"x": [1.0, 2.0, 3.0, 4.0], "y": [10.0, 20.0, 30.0, 40.0]})
    pos = run_step(df, CorrelationStep(
        step_id="s", op="correlation",
        params=CorrelationParams(column_x="x", column_y="y", method=CorrMethod.pearson)))
    assert pos.summary["coefficient"] == pytest.approx(1.0)
    df2 = pd.DataFrame({"x": [1.0, 2.0, 3.0, 4.0], "y": [40.0, 30.0, 20.0, 10.0]})
    neg = run_step(df2, CorrelationStep(
        step_id="s", op="correlation",
        params=CorrelationParams(column_x="x", column_y="y", method=CorrMethod.pearson)))
    assert neg.summary["coefficient"] == pytest.approx(-1.0)


def test_outlier_iqr_flags_sentinel():
    df = pd.DataFrame({"金额": [10.0, 12.0, 11.0, 13.0, 12.0, 500.0]})
    r = run_step(df, OutlierFlagStep(
        step_id="s", op="outlier_flag",
        params=OutlierFlagParams(column="金额", method=OutlierMethod.iqr, threshold=1.5)))
    assert r.df["is_outlier"].tolist() == [False, False, False, False, False, True]
    assert r.summary["outlier_count"] == 1


def test_non_numeric_metric_blocked(sales):
    with pytest.raises(EngineError, match="数值类型"):
        run_step(sales, AggregateStep(
            step_id="s", op="aggregate",
            params=AggregateParams(column="区域", func=AggFunc.sum)))


def test_missing_column_blocked(sales):
    with pytest.raises(EngineError, match="不存在"):
        run_step(sales, FilterStep(
            step_id="s", op="filter",
            params=FilterParams(column="幽灵列", operator=FilterOperator.eq, value="x")))


def test_engine_package_has_no_llm_import():
    """TR-10.2：引擎目录任何文件不得 import LLM 层（AST 检查真实导入）。"""
    import ast

    root = pathlib.Path(ops.__file__).parent
    for f in root.glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not any(m == "openai" or m.startswith("app.services.llm") for m in imported), (
            f"{f.name} 不得依赖 LLM 层"
        )


@pytest.mark.parametrize("op,chart", [
    ("aggregate", ChartType.metric),
    ("group_by", ChartType.bar),
    ("share", ChartType.share_bar),
    ("top_n", ChartType.bar),
    ("time_series", ChartType.line),
    ("period_compare", ChartType.metric_compare),
    ("compare_groups", ChartType.grouped_bar),
    ("correlation", ChartType.scatter),
    ("outlier_flag", ChartType.line_outlier),
])
def test_chart_mapping_matches_appendix_a(op, chart):
    """TR-10.3：末端算子 → 图表与附录 A 完全一致。"""
    assert CHART_BY_OP[op] == chart
    assert OP_META[op]["chart"] == chart
    assert OP_META["filter"]["terminal"] is False
