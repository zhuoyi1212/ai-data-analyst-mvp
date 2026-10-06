"""T12 评测 harness 冒烟测试。

不重复验证产品行为（T07/T08/T09 专项测试已覆盖），只保证：
1. 真值案例清单完整自洽（id 唯一、真值字段齐备）；
2. run_evaluation 能跑通 static/loop 双模式并产出契约化报告；
3. 黄金/阴性案例的聚合指标方向正确（Loop 定位命中、阴性干净拒绝、
   零预算违规、证据全接地）。
"""
from __future__ import annotations

import pytest

from scripts.evaluation.cases import CASES, CASES_BY_ID
from scripts.run_evaluation import CAPS, run_evaluation


def test_case_registry_is_self_consistent():
    assert len(CASES) == 9
    assert len(CASES_BY_ID) == len(CASES)  # id 唯一
    for case in CASES:
        assert callable(case.builder)
        assert case.builder()  # 确定性 builder 非空
        if case.expect_signal and case.case_type != "stress":
            assert case.truth_kind is not None
            if case.truth_kind == "branch":
                assert case.expected_dimension and case.expected_member
            elif case.truth_kind == "finding_keyword":
                assert case.expected_keywords
        if case.truth_kind == "relationship_absence":
            # 拒绝型真值只用于阴性案例；误报口径与数据处理策略必须显式声明
            assert case.expect_signal is False
            assert case.fp_policy == "relationship_only"
            assert case.quality_mode == "safe"


def test_evaluation_smoke_golden_and_negative(tmp_path):
    report = run_evaluation(
        case_ids=["known_issue_region_collapse", "no_problem_uniform"],
        store_root=tmp_path / "eval_store",
    )

    totals = report["totals"]
    assert totals["cases"] == 2
    assert totals["truth_cases"] == 1
    assert totals["negative_cases"] == 1
    assert totals["stress_cases"] == 0
    assert report["mode"] == "offline_rule_based"
    assert report["budget_caps"] == {
        "max_rounds": 4, "max_probes": 8,
        "max_terminal_executions": 24, "max_depth": 3}

    loop = report["metrics"]["loop"]
    assert loop["detection_rate"] == 1.0
    assert loop["false_positive_rate"] == 0.0
    assert loop["invalid_conclusion_rejection_rate"] == 1.0
    assert loop["evidence_grounding_rate"] == 1.0
    assert loop["budget_violations"] == 0
    assert loop["max_probes"] <= CAPS["max_probes"]

    rows = {r["case_id"]: r for r in report["cases"]}

    golden = rows["known_issue_region_collapse"]
    assert golden["loop"]["truth_hit"] is True
    assert golden["loop"]["hit_basis"] == "branch:Region=North"
    assert golden["loop"]["budget_violations"] == []

    negative = rows["no_problem_uniform"]
    assert negative["loop"]["clean_rejection"] is True
    assert negative["loop"]["stop_reason"] == "no_signal"
    assert negative["loop"]["expanded_branches"] == 0
    assert negative["static"]["findings"] == 0


def test_negative_guard_cases_spurious_sparse_partial(tmp_path):
    """假相关 / 稀疏小样本 / 残缺周期：三案例均不得产出相关结论或误报。"""
    report = run_evaluation(
        case_ids=[
            "spurious_correlation_single_outlier",
            "insufficient_data_sparse_pairs",
            "partial_period_mtd_no_signal",
        ],
        store_root=tmp_path / "eval_store",
    )

    totals = report["totals"]
    assert totals["cases"] == 3
    assert totals["negative_cases"] == 3
    loop = report["metrics"]["loop"]
    assert loop["false_positive_rate"] == 0.0
    # 残缺周期案例声明了 stop_reason 期望
    assert loop["stop_reason_expectation_rate"] == 1.0
    assert loop["stop_reason_checked_cases"] == 1
    assert loop["budget_violations"] == 0

    rows = {r["case_id"]: r for r in report["cases"]}

    spurious = rows["spurious_correlation_single_outlier"]
    for mode in ("static", "loop"):
        block = spurious[mode]
        assert block["truth_hit"] is True
        assert block["hit_basis"] == "no_relationship_claim"
        assert block["relationship_claims"] == 0
        assert block["consumable_relationship_views"] == 0

    sparse = rows["insufficient_data_sparse_pairs"]
    for mode in ("static", "loop"):
        assert sparse[mode]["relationship_claims"] == 0
        assert sparse[mode]["consumable_relationship_views"] == 0
        assert sparse[mode]["truth_hit"] is True

    partial = rows["partial_period_mtd_no_signal"]
    assert partial["loop"]["stop_reason"] == "no_signal"
    assert partial["loop"]["stop_reason_ok"] is True
    assert partial["loop"]["expanded_branches"] == 0
    assert partial["static"]["findings"] == 0
    assert partial["loop"]["findings"] == 0


def test_unknown_case_id_rejected(tmp_path):
    with pytest.raises(ValueError, match="未知案例"):
        run_evaluation(case_ids=["does_not_exist"], store_root=tmp_path)
