"""T07 结果价值筛选：执行后按六个维度评估每张视图并决定默认可见性。

评估维度（全部 0..1，确定性规则，LLM 不参与）：
    validity   数据有效性 —— 复用 View 门禁（pass 1.0 / warn 0.6 / 其余 0）；
    impact     业务影响 —— 指标业务价值、|r| 强度、离群占比；
    evidence   证据质量 —— 有效样本量（n、参与行数）；
    novelty    新证据 —— 同 (类型, 指标) 是否已存在；
    redundancy 冗余 —— 同 (业务族) 结果是否重复；
    display_friendliness 展示友好度 —— 输出行数越少越易读（越高分）。

默认可见性：
- presentation（Seed：核心 KPI / 双指标对齐趋势 / 一个基准拆分）始终默认可见，
  任何筛选都不得把它们挤掉（T07 验收：双指标趋势不被槽位挤掉）；
- computation（内部计算任务）默认隐藏，证据不足时给出明确理由：
    · relationship |r| < WEAK_ABS_R     —— 相关过弱（r=0 不占默认视图）；
    · anomaly      outlier_count == 0   —— 未发现异常；
    · 业务族重复                         —— 只有重复信息。
  隐藏不删除：结果保留在视图字典中，用户明确问到时仍可返回（含阴性结果）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.bundle import (
    AnalysisBundle,
    AnalysisView,
    BundleExecutionResult,
    ViewExecutionResult,
    ViewStatus,
    ViewType,
)
from app.services.bundle_planner import _metric_score

# 弱相关阈值：低于此值相关证据不占默认展示位（与 dashboard_insight.MIN_ABS_R 对齐）
WEAK_ABS_R = 0.30

_SCORE_KEYS = (
    "validity", "impact", "evidence", "novelty", "redundancy",
    "display_friendliness",
)


@dataclass(frozen=True)
class ValueAssessment:
    scores: dict[str, float]
    default_hidden: bool
    hide_reasons: list[str] = field(default_factory=list)


# ----------------------------------------------------------------- 内部工具

def _terminal_summary(er: ViewExecutionResult) -> dict:
    return er.steps[-1].summary if er.steps else {}


def _family_signature(view: AnalysisView) -> tuple:
    """业务族签名：覆盖 metric/维度，用于信息冗余判定（同业务族只留一个展示）。"""
    t = view.type
    metric = view.metric_fields[0] if view.metric_fields else None
    dim = view.dimension_fields[0] if view.dimension_fields else None
    if t in (ViewType.comparison, ViewType.breakdown, ViewType.ranking):
        return ("dist", dim, metric)
    if t is ViewType.profitability:
        return ("ratio", dim, tuple(view.metric_fields))
    if t is ViewType.relationship:
        # 与候选 family_key=("rel", x, y) 同构（非嵌套 tuple）
        if len(view.metric_fields) >= 2:
            return ("rel", view.metric_fields[0], view.metric_fields[1])
        return ("rel", tuple(view.metric_fields))
    if t is ViewType.overview:
        return ("overview", metric)
    if t is ViewType.trend:
        return ("trend", metric)
    if t is ViewType.anomaly:
        return ("anom", metric)
    if t is ViewType.contribution:
        return ("contrib", dim, metric)
    if t is ViewType.rate_shift:
        return ("rateshift", dim, tuple(view.metric_fields))
    return (t.value, dim, metric)


def _display_friendliness(rows: int) -> float:
    """展示友好度：输出行数越少越易读（越高分）。

    语义方向为正：rows 少 → 友好度高；行数多 → 友好度低。
    评分处正向加权，避免"越易读反而被惩罚"的反向语义。
    """
    if rows <= 12:
        return 1.0
    if rows <= 30:
        return 0.85
    if rows <= 100:
        return 0.7
    if rows <= 500:
        return 0.55
    return 0.4


# ----------------------------------------------------------------- 单视图评分

def _impact_score(view: AnalysisView, er: ViewExecutionResult, summary: dict) -> float:
    t = view.type
    if t is ViewType.relationship:
        r = summary.get("coefficient")
        return min(abs(float(r)) / 0.5, 1.0) if r is not None else 0.2
    if t is ViewType.anomaly:
        count = int(summary.get("outlier_count", 0))
        rate = float(summary.get("outlier_rate", 0.0))
        return min(1.0, count / 10.0 + rate * 5.0)
    if t is ViewType.contribution:
        # 相对变化强度：20% 变化即满分；基期为 0 时只看是否有绝对变化
        base = abs(float(summary.get("total_base_value", 0.0)))
        delta = abs(float(summary.get("total_delta", 0.0)))
        rel = delta / base if base > 1e-9 else (1.0 if delta > 1e-9 else 0.0)
        return min(rel / 0.20, 1.0) if rel else 0.2
    if t is ViewType.rate_shift:
        pp = abs(float(summary.get("change_pp", 0.0)))  # 整体率变化（百分点）
        return min(pp / 5.0, 1.0) if pp else 0.2
    metric = view.metric_fields[0] if view.metric_fields else ""
    return max(0.25, _metric_score(metric) / 4.0)


def _evidence_score(view: AnalysisView, er: ViewExecutionResult, summary: dict) -> float:
    if view.type is ViewType.relationship:
        n = summary.get("n")
        return min(float(n) / 60.0, 1.0) if n is not None else 0.3
    return min(float(er.participating_rows) / 200.0, 1.0)


def assess_view(view: AnalysisView, er: ViewExecutionResult) -> dict[str, float]:
    """只评分（0..1），不决定可见性；便于单测与复用。"""
    summary = _terminal_summary(er)
    validity = {"pass": 1.0, "warn": 0.6}.get(er.validity, 0.0)
    impact = _impact_score(view, er, summary)
    evidence = _evidence_score(view, er, summary)
    novelty = 1.0  # 集体过滤时按实际重复情况下调
    redundancy = 1.0
    display_friendliness = _display_friendliness(er.result_rows_total)
    return {
        "validity": validity, "impact": impact, "evidence": evidence,
        "novelty": novelty, "redundancy": redundancy,
        "display_friendliness": display_friendliness,
    }


# ----------------------------------------------------------------- 整批过滤

def filter_run(
    bundle: AnalysisBundle, execution: BundleExecutionResult
) -> dict[str, ValueAssessment]:
    """对一次运行的全部视图做价值评估，返回 view_id → ValueAssessment。

    - presentation 视图：default_hidden 恒为 False（Seed 保证）；
    - computation 视图：默认隐藏；弱 r / 无异常 / 冗余给出具体理由。
    """
    results = {r.view_id: r for r in execution.views}
    views = {v.view_id: v for v in bundle.analysis_views}

    seen_families: dict[tuple, str] = {}
    seen_type_metric: set[tuple] = set()
    out: dict[str, ValueAssessment] = {}

    for view in bundle.analysis_views:
        er = results.get(view.view_id)
        if er is None:
            out[view.view_id] = ValueAssessment(
                scores={k: 0.0 for k in _SCORE_KEYS},
                default_hidden=True,
                hide_reasons=["该视角未返回执行结果。"],
            )
            continue

        scores = assess_view(view, er)
        reasons: list[str] = []

        # 冗余：与已出现视图同业务族
        sig = _family_signature(view)
        prior_family = seen_families.get(sig)
        if prior_family is not None:
            scores["redundancy"] = 0.0
            reasons.append(f"与「{prior_family}」指向同一业务族，信息重复")

        # 新证据：同类型同指标已出现（非业务族重复时的轻度降权）
        tm = (view.type, view.metric_fields[0] if view.metric_fields else None)
        if tm in seen_type_metric:
            scores["novelty"] = 0.4
        seen_type_metric.add(tm)

        # 特定证据规则（使用末端算子摘要，数字来自引擎，不新造）
        summary = _terminal_summary(er)
        if view.type is ViewType.relationship:
            r = summary.get("coefficient")
            if r is None:
                reasons.append("未取得相关系数，证据不足")
            elif abs(float(r)) < WEAK_ABS_R:
                reasons.append(
                    f"相关系数过弱（r={float(r):.2f}），默认不展示"
                )
        if view.type is ViewType.anomaly:
            count = summary.get("outlier_count")
            if count is not None and int(count) == 0:
                reasons.append("未发现离群值，默认不展示")
        if view.type is ViewType.contribution:
            base = summary.get("total_base_value")
            delta = summary.get("total_delta")
            if delta is not None and abs(float(delta)) <= 1e-9:
                reasons.append("总指标无变化，默认不展示")
            elif (
                base is not None and delta is not None
                and abs(float(base)) > 1e-9
                and abs(float(delta)) / abs(float(base)) < 0.05
            ):
                reasons.append("总指标环比变化不足 5%，信号不显著，默认不展示")
        if view.type is ViewType.rate_shift:
            pp = summary.get("change_pp")
            if pp is not None and abs(float(pp)) < 1.0:
                reasons.append("整体率变化不足 1 个百分点，默认不展示")

        if prior_family is None and er.consumable:
            seen_families[sig] = view.title

        if view.role == "presentation":
            # Seed 视图永远不被隐藏（即使证据偏弱也按用户要求的核心视角展示）
            hidden = False
        else:
            hidden = True
            if not reasons:
                reasons.append("内部计算任务，默认不占展示位，需要时可查看")

        out[view.view_id] = ValueAssessment(
            scores=scores, default_hidden=hidden, hide_reasons=reasons,
        )
    return out
