"""T12 第 2 项 / T10 欠账：相关结论留一（leave-one-out）稳定性门禁专项测试。

三层覆盖：
1. 算子层 _correlation_sensitivity：单点主导 / 稳定 / 小样本 / 大样本近似 / 常量列；
2. 执行器层 _relationship_stability_check：假相关与小样本 relationship 视图 fail；
3. 端到端：假相关完整流水线不产相关结论，但离群点 anomaly 告警仍然合法。

确定性：不连任何服务、不调 LLM。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.schemas.bundle import AnalysisView, ViewType
from app.schemas.common import ChartType, CorrMethod
from app.schemas.ledger import StepRecord
from app.schemas.plan import (
    AnalysisPlan,
    CorrelationParams,
    CorrelationStep,
)
from app.services.bundle_executor import (
    _checks,
    _relationship_stability_check,
)
from app.services.engine.ops import (
    MIN_CORRELATION_PAIRS,
    _correlation_sensitivity,
    run_step,
)
from scripts.evaluation.cases import CASES_BY_ID
from scripts.run_evaluation import _prepare, _relationship_claims
from app.services.dashboard_synthesizer import synthesize_dashboard


# --------------------------------------------------------------- 测试数据


def _spurious_pairs() -> pd.DataFrame:
    """29 个与 X 正交的 Y 点（总体 r=0）+ 第 30 点 (120,120)。"""
    n_base = 29
    x = list(range(n_base)) + [120.0]
    y = [1.0 if i % 2 == 0 else -1.0 for i in range(n_base)] + [120.0]
    return pd.DataFrame({"x": x, "y": y})


def _stable_pairs(n: int = 60, seed: int = 7) -> pd.DataFrame:
    """带小幅噪声的稳定线性关系。"""
    rng = np.random.default_rng(seed)
    x = np.linspace(0, 10, n)
    y = 2.0 * x + 1.0 + rng.normal(0, 0.15, n)
    return pd.DataFrame({"x": x, "y": y})


# --------------------------------------------------------------- 1. 算子层


def test_spurious_correlation_flagged_single_point_driven():
    pairs = _spurious_pairs()
    step = run_step(pairs, CorrelationStep(
        step_id="s", op="correlation",
        params=CorrelationParams(column_x="x", column_y="y",
                                 method=CorrMethod.pearson)))
    r = step.summary["coefficient"]
    assert r >= 0.70  # 全样本看似强相关

    sens = step.summary["sensitivity"]
    assert sens["status"] == "single_point_driven"
    assert sens["kind"] == "leave_one_out"
    assert sens["loo_exact"] is True
    assert sens["most_influential_index"] == 29  # 第 30 个点（0-based）
    assert abs(sens["r_without_most_influential"]) < 0.50
    assert sens["max_abs_delta_r"] >= 0.20
    # 剔除该点后真实相关 ≈ 0
    assert sens["r_without_most_influential"] == 0.0
    # 原始字段不受影响：算子仍如实给出原始 r 与 n
    assert step.summary["n"] == 30
    assert step.summary["method"] == "pearson"
    assert "异常点" in sens["note"]


def test_stable_linear_passes_leave_one_out():
    pairs = _stable_pairs()
    step = run_step(pairs, CorrelationStep(
        step_id="s", op="correlation",
        params=CorrelationParams(column_x="x", column_y="y",
                                 method=CorrMethod.pearson)))
    assert step.summary["coefficient"] > 0.95
    sens = step.summary["sensitivity"]
    assert sens["status"] == "ok"
    assert sens["loo_exact"] is True
    assert sens["max_abs_delta_r"] < 0.05
    assert "留一检验通过" in sens["note"]


def test_small_sample_marked_insufficient_without_loo():
    pairs = pd.DataFrame({"x": [1.0, 2, 3, 4, 5, 6],
                          "y": [2.0, 4, 6, 8, 10, 12]})  # r=1 巧合
    step = run_step(pairs, CorrelationStep(
        step_id="s", op="correlation",
        params=CorrelationParams(column_x="x", column_y="y",
                                 method=CorrMethod.pearson)))
    assert step.summary["coefficient"] == 1.0  # 算子不撒谎，但…
    sens = step.summary["sensitivity"]
    assert sens["status"] == "insufficient_n"
    assert sens["min_pairs_for_claim"] == MIN_CORRELATION_PAIRS
    assert sens["most_influential_index"] is None
    assert f"有效样本仅 6 对" in sens["note"]


def test_spearman_large_sample_uses_approximation_without_crashing():
    rng = np.random.default_rng(11)
    x = np.arange(300, dtype=float)
    y = x * 0.5 + rng.normal(0, 1.0, 300)
    pairs = pd.DataFrame({"x": x, "y": y})
    step = run_step(pairs, CorrelationStep(
        step_id="s", op="correlation",
        params=CorrelationParams(column_x="x", column_y="y",
                                 method=CorrMethod.spearman)))
    sens = step.summary["sensitivity"]
    assert sens["status"] == "ok"
    assert sens["loo_exact"] is False  # n>200 走全样本秩近似
    assert "近似" in sens["note"]


def test_spearman_is_naturally_robust_to_single_outlier():
    """秩对单点离群天然稳健：同一份假相关数据 Spearman 不应误报为单点主导。"""
    pairs = _spurious_pairs()
    step = run_step(pairs, CorrelationStep(
        step_id="s", op="correlation",
        params=CorrelationParams(column_x="x", column_y="y",
                                 method=CorrMethod.spearman)))
    assert abs(step.summary["coefficient"]) < 0.50
    assert step.summary["sensitivity"]["status"] == "ok"


def test_constant_column_is_inconclusive_not_ok():
    pairs = pd.DataFrame({"x": [1.0] * 12,
                          "y": [float(i) for i in range(12)]})
    sens = _correlation_sensitivity(
        "pearson", pairs, r=float("nan"))
    assert sens["status"] == "inconclusive"
    assert sens["most_influential_index"] is None


# --------------------------------------------------------------- 2. 执行器门禁


def _relationship_view() -> AnalysisView:
    return AnalysisView(
        view_id="view_rel", title="X 与 Y 的关系", type=ViewType.relationship,
        question="X 和 Y 是否相关？",
        plan=AnalysisPlan(
            question="X 和 Y 是否相关？",
            steps=[CorrelationStep(
                step_id="corr", op="correlation",
                params=CorrelationParams(
                    column_x="x", column_y="y", method=CorrMethod.pearson))]),
        chart=ChartType.scatter,
        metric_fields=["x", "y"],
    )


def _record(summary: dict, *, input_rows: int | None = None) -> StepRecord:
    return StepRecord(
        step_id="corr", op="correlation",
        params={"column_x": "x", "column_y": "y", "method": "pearson"},
        input_rows=input_rows if input_rows is not None else summary["n"],
        output_rows=1, output_columns=["coefficient"],
        formula="相关分析", summary=summary,
    )


def test_executor_rejects_small_n_relationship_view():
    view = _relationship_view()
    record = _record({"n": 6, "coefficient": 1.0, "method": "pearson",
                      "sensitivity": {"status": "insufficient_n"}})
    item = _relationship_stability_check(view, [record])
    assert item is not None
    assert item.code == "relationship_stability"
    assert item.level == "fail"
    assert "有效样本仅 6 对" in item.detail
    assert item.numbers["n"] == 6


def test_executor_rejects_single_point_driven_view():
    view = _relationship_view()
    record = _record({
        "n": 30, "coefficient": 0.9169, "method": "pearson",
        "sensitivity": {
            "status": "single_point_driven",
            "r_without_most_influential": 0.0,
            "max_abs_delta_r": 0.9169,
            "most_influential_index": 29,
        },
    }, input_rows=30)
    item = _relationship_stability_check(view, [record])
    assert item is not None and item.level == "fail"
    assert "单个异常点主导" in item.detail
    assert "第 30 对" in item.detail
    assert item.numbers["most_influential_index"] == 29


def test_executor_allows_stable_relationship_and_ignores_other_types():
    view = _relationship_view()
    record = _record({
        "n": 60, "coefficient": 0.98, "method": "pearson",
        "sensitivity": {"status": "ok", "max_abs_delta_r": 0.002},
    }, input_rows=60)
    assert _relationship_stability_check(view, [record]) is None

    trend_view = view.model_copy(update={"type": ViewType.trend})
    assert _relationship_stability_check(trend_view, [record]) is None
    assert _relationship_stability_check(view, []) is None


def test_checks_append_stability_failure():
    view = _relationship_view()
    record = _record({"n": 6, "coefficient": 1.0, "method": "pearson",
                      "sensitivity": {"status": "insufficient_n"}})
    result = pd.DataFrame({"coefficient": [1.0]})
    checks = _checks(view, result, participating_rows=6, records=[record])
    stability = [c for c in checks if c.code == "relationship_stability"]
    assert len(stability) == 1 and stability[0].level == "fail"


# --------------------------------------------------------------- 3. 端到端


def test_e2e_spurious_data_blocks_relationship_but_keeps_anomaly(tmp_path):
    """假相关完整流水线：无相关结论；离群点 anomaly 告警合法保留。"""
    case = CASES_BY_ID["spurious_correlation_single_outlier"]
    pre = _prepare(case, tmp_path / "spurious")
    artifact = synthesize_dashboard(pre["sid"], pre["store"])

    assert _relationship_claims(artifact) == []
    rel_views = [v for v in artifact.views.values()
                 if v.type is ViewType.relationship]
    assert rel_views  # 散点视图仍保留在字典中
    assert all(not v.consumable for v in rel_views)
    # 真实的极端离群点仍应被异常分析捕获（不是把孩子和洗澡水一起倒掉）
    assert any("异常" in t or "离群" in t for t in (f.title for f in artifact.findings))
