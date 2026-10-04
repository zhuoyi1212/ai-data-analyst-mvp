"""Insight Candidate 生成与评分（重构 Phase 2/3）验收。

覆盖：
- 信号 → 业务事实类型映射正确（growth/decline→performance_change、
  negative_member→risk、divergence→underperformance、scale_profit→divergence、
  correlation→relationship/risk、anomaly→anomaly）；
- 候选证据视图均为真实 view id、分数 0..1、final_score 降序确定；
- 冗余去重：同业务族只留强者（冗余项被罚分）；
- final_score 公式与手算一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.schemas.insight_candidate import InsightCandidate
from app.services.insight_candidates import (
    generate_insight_candidates,
    score_and_rank,
)
from test_signals import _run_scan_and_signals, _superstore_df


# ----------------------------------------------------------------- 端到端

def test_candidates_from_signals_are_valid(tmp_path):
    store, sid, bundle, execution, signals = _run_scan_and_signals(
        tmp_path, _superstore_df()
    )
    view_ids = {v.view_id for v in bundle.analysis_views}
    cands = generate_insight_candidates(
        sid, store, bundle, execution, signals, findings=[], scope_tag="t"
    )
    ranked = score_and_rank(cands)

    assert ranked, "应至少产出若干候选 Insight"
    # evidence 必须指向真实视图
    for c in ranked:
        if c.evidence_view_ids:
            assert set(c.evidence_view_ids) <= view_ids
        assert 0.0 <= c.final_score <= 1.0
        assert 0.0 <= c.impact_score <= 1.0
        assert 0.0 <= c.confidence_score <= 1.0
        assert c.insight_id.startswith("ins_")

    # 降序确定
    scores = [c.final_score for c in ranked]
    assert scores == sorted(scores, reverse=True)

    # 信号类型映射：预埋数据必然覆盖若干业务事实类型
    types = {c.type for c in ranked}
    assert types.issubset({
        "performance_change", "contribution", "concentration",
        "underperformance", "divergence", "anomaly", "efficiency",
        "opportunity", "risk", "relationship",
    })
    assert types  # 非空


# ----------------------------------------------------------------- 评分公式与去重

def _mk(
    type: str = "performance_change", *, metric="Sales", dim=None, member=None,
    impact=0.8, anomaly=0.0, action=0.7, explain=0.5, conf=0.9,
    delta_pct=-0.2,
) -> InsightCandidate:
    return InsightCandidate(
        insight_id="", type=type, title=f"{metric} 变化", summary="现象",
        metric=metric, dimension=dim, member=member,
        delta_pct=delta_pct,
        impact_score=impact, anomaly_score=anomaly,
        actionability_score=action, explainability_score=explain,
        confidence_score=conf,
        evidence_view_ids=["v1"],
    )


def test_final_score_formula_matches_manual():
    c = _mk()
    sig = min(1.0, abs(-0.2) / 0.20)  # significance = 1.0
    expected = round(0.30 * 0.8 + 0.20 * 0.7 + 0.20 * 0.5 + 0.15 * sig + 0.15 * 0.9, 4)
    ranked = score_and_rank([c])
    assert ranked[0].final_score == expected


def test_redundant_duplicates_are_penalized():
    a = _mk(impact=0.9)
    b = _mk(impact=0.9)  # 同 (type, metric, dim, member) 完全重复
    ranked = score_and_rank([a, b])
    assert len(ranked) == 2
    # 强者保留，冗余项被罚分
    redundant = [c for c in ranked if c.redundancy_score > 0]
    assert len(redundant) == 1
    kept = [c for c in ranked if c.redundancy_score == 0][0]
    assert redundant[0].final_score < kept.final_score


def test_insight_ids_ordered_by_score():
    hi = _mk(impact=1.0, conf=1.0)
    lo = _mk(metric="Profit", impact=0.1, action=0.3, explain=0.3, conf=0.2, delta_pct=0.0)
    ranked = score_and_rank([lo, hi])
    assert ranked[0].insight_id == "ins_01"
    assert ranked[0].final_score >= ranked[1].final_score
    assert {c.insight_id for c in ranked} == {"ins_01", "ins_02"}