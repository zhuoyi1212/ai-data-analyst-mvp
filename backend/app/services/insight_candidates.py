"""Insight Candidate 生成与结果驱动评分（重构 Phase 2/3）。

流水线（确定性，LLM 不参与判定，只允许后续做语义改写）：
    1. 从已提取的 Signal 抽象「业务事实」候选（类型映射见 _SIGNAL_TO_INSIGHT）；
    2. 从 contribution / breakdown 视图补充「贡献」「集中度」两类候选；
    3. 计算结果驱动子评分（impact/anomaly/actionability/explainability/confidence）；
    4. 冗余去重（同业务族只留强者），合成 final_score 并排序。

final_score = 0.30*impact + 0.20*actionability + 0.20*explainability
            + 0.15*significance + 0.15*confidence - redundancy_penalty
其中 significance = max(anomaly_score, 变化幅度归一化)。
所有分数 0..1；排序确定性（final_score → type → metric → member）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.schemas.auto import Signal
from app.schemas.bundle import (
    AnalysisBundle,
    BundleExecutionResult,
    ViewType,
)
from app.schemas.common import ChartType
from app.schemas.dashboard import Finding
from app.schemas.insight_candidate import InsightCandidate, InsightImportance
from app.services.dashboard_insight import _load
from app.services.storage import SessionStore

# 信号类型 → 业务事实类型（1:1 确定性映射；correlation 按符号二分）
_SIGNAL_TO_INSIGHT: dict[str, str] = {
    "growth": "performance_change",
    "decline": "performance_change",
    "negative_member": "risk",
    "divergence": "underperformance",
    "scale_profit": "divergence",
    "simpson": "divergence",
    "anomaly": "anomaly",
}

# 业务事实 → 推荐图表（Insight → Chart，服务结论而非图表多样性）
_CHART_FOR_TYPE: dict[str, ChartType] = {
    "performance_change": ChartType.line,
    "contribution": ChartType.bar,
    "concentration": ChartType.share_bar,
    "underperformance": ChartType.bar,
    "divergence": ChartType.scatter,
    "anomaly": ChartType.line_outlier,
    "opportunity": ChartType.line,
    "risk": ChartType.bar,
    "relationship": ChartType.scatter,
    "efficiency": ChartType.bar,
}

# 变化幅度 → significance 归一化（20% 变化即满分）
_SIG_FULL = 0.20
# 集中度候选阈值：Top3 份额达到 60% 才算「过度集中」
_CONCENTRATION_MIN = 0.60

# 子评分：可行动性（能否指向具体区域/产品/渠道/客群/时间）
_ACTIONABILITY = {
    "contribution": 0.9,
    "underperformance": 0.9,
    "risk": 0.8,
    "divergence": 0.8,
    "concentration": 0.7,
    "performance_change": 0.5,
    "relationship": 0.4,
    "anomaly": 0.7,
    "efficiency": 0.8,
    "opportunity": 0.8,
}

# 子评分：可解释性（能否回答「为什么发生变化」）
_EXPLAINABILITY = {
    "contribution": 0.95,   # 直接说明谁贡献
    "underperformance": 0.9,
    "divergence": 0.9,
    "concentration": 0.75,
    "risk": 0.8,
    "performance_change": 0.5,  # 只说什么变了，未解释为何
    "relationship": 0.55,       # 相关非因果
    "anomaly": 0.4,
    "efficiency": 0.85,
    "opportunity": 0.65,
}

@dataclass
class _Draft:
    """聚合中间态：Signal 或视图派生候选统一结构。"""

    type: str
    title: str
    summary: str
    metric: str | None = None
    dimension: str | None = None
    member: str | None = None
    current_value: float | None = None
    comparison_value: float | None = None
    delta: float | None = None
    delta_pct: float | None = None
    evidence_view_ids: list[str] = field(default_factory=list)
    importance: InsightImportance = "medium"
    impact: float = 0.0
    anomaly: float = 0.0
    confidence: float = 0.7


# ----------------------------------------------------------------- 工具

def _consumable_views(bundle: AnalysisBundle, execution: BundleExecutionResult):
    ok = {r.view_id for r in execution.views if r.consumable}
    return {v.view_id: v for v in bundle.analysis_views if v.view_id in ok}


def _finding_for(
    views: list[str], findings: list[Finding]
) -> Finding | None:
    """信号证据视图命中的第一条 Finding（用于复用其结论式标题/四要素摘要）。"""
    for f in findings:
        if any(v in f.evidence_view_ids for v in views):
            return f
    return None


# ----------------------------------------------------------------- Signal → 候选


def _signals_to_drafts(signals: list[Signal], findings: list[Finding]) -> list[_Draft]:
    drafts: list[_Draft] = []
    for sig in signals:
        insight_type = _SIGNAL_TO_INSIGHT.get(sig.type)
        if insight_type is None:
            if sig.type == "correlation":
                r = sig.magnitude.coefficient
                insight_type = "relationship" if (r or 0) >= 0 else "risk"
            else:
                continue
        f = _finding_for(sig.scan_view_ids, findings)
        mag = sig.magnitude
        title = f.title if f is not None else f"{sig.metric} 出现显著变化"
        summary = (
            f.summary if f is not None
            else f"现象：{sig.metric} 出现 {sig.type} 信号，详情见证据视图。"
        )
        drafts.append(_Draft(
            type=insight_type,
            title=title,
            summary=summary,
            metric=sig.metric,
            dimension=sig.dimension,
            member=sig.member,
            current_value=mag.value,
            comparison_value=mag.compare_value,
            delta=(
                (mag.value - mag.compare_value)
                if mag.value is not None and mag.compare_value is not None
                else None
            ),
            delta_pct=(
                mag.change_pct / 100.0
                if mag.change_pct is not None else None
            ),
            evidence_view_ids=list(sig.scan_view_ids),
            importance="high" if sig.score >= 0.5 else "medium",
            impact=sig.score_breakdown.impact,
            anomaly=1.0 if insight_type == "anomaly" else 0.0,
            confidence=sig.score_breakdown.support,
        ))
    return drafts


# ----------------------------------------------------------------- 视图派生候选


def _contribution_drafts(
    sid: str, store: SessionStore, views: dict[str, Any],
) -> list[_Draft]:
    """谁贡献了整体变化：contribution 视图中 |贡献额| 最大的成员。"""
    drafts: list[_Draft] = []
    for v in views.values():
        if v.type is not ViewType.contribution or not v.metric_fields:
            continue
        dim = v.dimension_fields[0] if v.dimension_fields else None
        if dim is None:
            continue
        df = _load(store, sid, v.view_id)
        if "delta" not in df.columns or dim not in df.columns:
            continue
        total = float(df["delta"].sum())
        if abs(total) <= 1e-9:
            continue
        row = df.reindex(df["delta"].abs().sort_values(ascending=False).index).iloc[0]
        member = str(row[dim])
        d = float(row["delta"])
        share = d / total  # 贡献占比（可负）
        metric = v.metric_fields[0]
        drafts.append(_Draft(
            type="contribution",
            title=f"「{member}」贡献了整体 {metric} 变化的 {abs(share) * 100:.0f}%",
            summary=(
                f"现象：按「{dim}」看，成员「{member}」对 {metric} 的整体变化贡献最大"
                f"（{d:+,.0f}），占整体变化 {abs(share) * 100:.0f}%。"
                f"建议：围绕「{member}」优先复盘增长/下滑来源。"
            ),
            metric=metric,
            dimension=dim,
            member=member,
            delta=d,
            delta_pct=share,
            evidence_view_ids=[v.view_id],
            importance="high" if abs(share) >= 0.4 else "medium",
            impact=min(1.0, abs(share)),
            confidence=0.85,
        ))
    return drafts


def _concentration_drafts(
    sid: str, store: SessionStore, views: dict[str, Any],
) -> list[_Draft]:
    """结构是否过度集中：Top3 份额达到阈值才产出。"""
    drafts: list[_Draft] = []
    for v in views.values():
        if v.type not in (ViewType.breakdown, ViewType.comparison):
            continue
        if not v.metric_fields or not v.dimension_fields:
            continue
        dim = v.dimension_fields[0]
        df = _load(store, sid, v.view_id)
        if dim not in df.columns or "value" not in df.columns:
            continue
        vals = df["value"].dropna()
        if len(vals) < 4:  # 类别过少时集中是必然，不算业务发现
            continue
        total = float(vals.sum())
        if total <= 1e-9:
            continue
        if (vals < 0).any():
            # 混合符号指标（如利润含亏损成员）金额求和会坍缩，份额语义失真，
            # 「Top 3 贡献 X%」不再是有效业务事实。
            continue
        top3 = float(vals.sort_values(ascending=False).head(3).sum())
        share = top3 / total
        if share < _CONCENTRATION_MIN:
            continue
        metric = v.metric_fields[0]
        top_members = df.reindex(
            vals.sort_values(ascending=False).head(3).index
        )
        top3_names = "、".join(str(m) for m in top_members[dim].tolist())
        drafts.append(_Draft(
            type="concentration",
            title=f"Top 3 「{dim}」贡献 {metric} 的 {share * 100:.0f}%",
            summary=(
                f"现象：{metric} 高度集中于「{dim}」头部成员（{top3_names}，"
                f"合计 {share * 100:.0f}%）。"
                f"建议：评估对头部成员的依赖风险，判断是否需分散经营。"
            ),
            metric=metric,
            dimension=dim,
            current_value=share,
            evidence_view_ids=[v.view_id],
            importance="high" if share >= 0.7 else "medium",
            impact=min(1.0, share / 0.7),
            confidence=0.9,
        ))
    return drafts


# ----------------------------------------------------------------- 生成

def generate_insight_candidates(
    session_id: str,
    store: SessionStore,
    bundle: AnalysisBundle,
    execution: BundleExecutionResult,
    signals: list[Signal],
    findings: list[Finding],
    *,
    scope_tag: str = "",
) -> list[InsightCandidate]:
    """Broad Scan 之后、合成之前：把信号与视图结果抽象为业务事实候选。

    只做聚合与结构化，不新造数字；final_score 由 score_and_rank 统一结算。
    """
    views = _consumable_views(bundle, execution)
    drafts = _signals_to_drafts(signals, findings)
    drafts.extend(_contribution_drafts(session_id, store, views))
    drafts.extend(_concentration_drafts(session_id, store, views))

    candidates: list[InsightCandidate] = []
    for d in drafts:
        # actionability 由类型基线 × 成员聚焦度加权（可落到具体成员>维度>整体）
        member_factor = 1.0 if d.member else (0.85 if d.dimension else 0.7)
        actionability = round(
            max(0.0, min(1.0, _ACTIONABILITY.get(d.type, 0.5) * member_factor)), 4
        )
        explainability = round(_EXPLAINABILITY.get(d.type, 0.5), 4)
        candidates.append(InsightCandidate(
            insight_id="",  # score_and_rank 统一编号
            type=d.type,  # type: ignore[arg-type]
            title=d.title,
            summary=d.summary,
            metric=d.metric,
            scope=scope_tag,
            dimension=d.dimension,
            member=d.member,
            current_value=d.current_value,
            comparison_value=d.comparison_value,
            delta=d.delta,
            delta_pct=d.delta_pct,
            impact_score=round(max(0.0, min(1.0, d.impact)), 4),
            anomaly_score=round(max(0.0, min(1.0, d.anomaly)), 4),
            actionability_score=actionability,
            explainability_score=explainability,
            confidence_score=round(max(0.0, min(1.0, d.confidence)), 4),
            evidence_view_ids=list(d.evidence_view_ids),
            recommended_chart_type=_CHART_FOR_TYPE.get(d.type),
            importance=d.importance,
        ))
    return candidates


# ----------------------------------------------------------------- 评分 / 排序 / 去重


def _significance(c: InsightCandidate) -> float:
    """异常/显著性：异常候选取 anomaly_score；变化类取 |delta_pct| 归一化。"""
    if c.anomaly_score > 0:
        return c.anomaly_score
    if c.delta_pct is not None:
        return min(1.0, abs(c.delta_pct) / _SIG_FULL)
    if c.type == "concentration" and c.current_value is not None:
        return min(1.0, c.current_value / 0.7)
    return 0.0


def _dedup_key(c: InsightCandidate) -> tuple:
    """业务族去重键：同 (类型, 指标, 维度, 成员) 视为同一件事。"""
    return (c.type, c.metric or "", c.dimension or "", c.member or "")


def score_and_rank(
    candidates: list[InsightCandidate],
) -> list[InsightCandidate]:
    """冗余去重 + 合成 final_score + 确定性排序，返回赋号后的候选列表。"""
    # 1) 冗余：同业务族只留 impact 最高者（同族其余候选后续记冗余罚分）
    by_key: dict[tuple, InsightCandidate] = {}
    for c in candidates:
        key = _dedup_key(c)
        if key not in by_key:
            by_key[key] = c

    # 2) final_score
    for c in candidates:
        sig = _significance(c)
        key = _dedup_key(c)
        redundant = key in by_key and by_key[key] is not c
        c.redundancy_score = 0.4 if redundant else 0.0
        c.final_score = round(max(0.0, min(1.0, (
            0.30 * c.impact_score
            + 0.20 * c.actionability_score
            + 0.20 * c.explainability_score
            + 0.15 * sig
            + 0.15 * c.confidence_score
            - c.redundancy_score
        ))), 4)
        c.reason = (
            f"impact={c.impact_score:.2f} action={c.actionability_score:.2f} "
            f"explain={c.explainability_score:.2f} sig={sig:.2f} "
            f"conf={c.confidence_score:.2f} redund={c.redundancy_score:.2f}"
            + ("（业务族冗余，保留强者）" if redundant else "")
        )

    # 3) 确定性排序
    order = sorted(
        candidates,
        key=lambda c: (
            -c.final_score,
            c.type,                      # type 作为次级决胜键（确定性）
            c.metric or "",
            c.member or "",
        ),
    )

    # 4) 统一编号（排序后）
    for i, c in enumerate(order, start=1):
        c.insight_id = f"ins_{i:02d}"
    return order