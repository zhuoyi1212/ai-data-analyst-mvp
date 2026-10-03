"""Broad Scan 广度扫描器（Task 2）。

八类广扫（数量随字段条件自适应，无对应字段则类别缺席）：
    1. kpi          核心 KPI（每个主指标总览，至多 3）
    2. trend        时间趋势（主指标对齐趋势，至多 2）
    3. comparison   主要维度对比（至多 6）
    4. top_bottom   Top / Bottom 对称排名（至多 4）
    5. share        结构占比（至多 4）
    6. anomaly      异常离群（主指标，至多 2）
    7. relationship 指标关系（最佳相关对，1）
    8. profitability 盈利/效率（确定性派生比率，至多 3）

复用 bundle_planner 的 CandidateGenerator（口径/family 工具完全一致），
新增 Bottom 对称视角与第二指标离群；所有视图仍走 execute_bundle 与既有校验门禁。
产物为标准 AnalysisBundle——下游 Signal Detection（Task 3）只消费执行结果。
"""
from __future__ import annotations

import uuid
from dataclasses import replace

import pandas as pd

from app.schemas.bundle import AnalysisBundle, ViewType
from app.schemas.dictionary import DataDictionary, FieldProfile
from app.services.bundle_planner import (
    Candidate,
    CandidateGenerator,
    _ranked_metrics,
    _to_view,
)

MAX_SCAN_VIEWS = 24

# 类别 → 视图类型；最后两类为内部证据视角
_CATEGORY_OF_TYPE: dict[ViewType, str] = {
    ViewType.overview: "kpi",
    ViewType.trend: "trend",
    ViewType.comparison: "comparison",
    ViewType.ranking: "top_bottom",
    ViewType.breakdown: "share",
    ViewType.anomaly: "anomaly",
    ViewType.relationship: "relationship",
    ViewType.profitability: "profitability",
    # 内部证据视角（为 Signal Detection 的 Simpson/贡献信号提供真实 scan 证据）
    ViewType.contribution: "contribution",
    ViewType.rate_shift: "rate_shift",
}

_CATEGORY_ORDER = [
    "kpi", "trend", "comparison", "top_bottom", "share",
    "anomaly", "relationship", "profitability",
    "contribution", "rate_shift",
]
_CATEGORY_QUOTA = {
    "kpi": 3, "trend": 2, "comparison": 6, "top_bottom": 4,
    "share": 4, "anomaly": 2, "relationship": 1, "profitability": 3,
    "contribution": 2, "rate_shift": 2,
}


# ---------------------------------------------------------------- 候选增强


def _bottom_twin(c: Candidate) -> Candidate:
    """Top 排名候选 → 对称 Bottom 候选（order=asc）。"""
    hint = dict(c.hint)
    hint["order"] = "asc"
    n = hint.get("n", 5)
    dim = hint["dimension"]
    metric = hint.get("metric")
    return replace(
        c,
        hint=hint,
        title=f"「{dim}」「{metric}」Bottom {n}",
        question=f"「{metric}」最低的 {n} 个「{dim}」是谁？",
        family_key=("dist-bottom", dim, metric),
        reason=f"与 Top {n} 对称：定位「{metric}」最差的 {n} 个「{dim}」",
    )


def _anomaly_twin(c: Candidate, metric: FieldProfile) -> Candidate:
    """主指标离群候选 → 第二指标离群候选。"""
    return replace(
        c,
        title=f"「{metric.name}」离群检测",
        question=f"「{metric.name}」是否存在需要关注的异常值？",
        hint={"column": metric.name},
        fields=[metric.name],
        metric_fields=[metric.name],
        family_key=("anom", metric.name),
        reason=f"IQR 1.5 倍规则识别第二主指标「{metric.name}」离群",
    )


def _augment(
    candidates: list[Candidate],
    metrics: list[FieldProfile],
    df: pd.DataFrame | None,
) -> list[Candidate]:
    """在既有候选上补齐 Bottom 对称视角与第二指标离群（保持扫描顺序）。"""
    out: list[Candidate] = []
    for c in candidates:
        out.append(c)
        if c.type is ViewType.ranking:
            out.append(_bottom_twin(c))
        if c.type is ViewType.anomaly and len(metrics) > 1:
            out.append(_anomaly_twin(c, metrics[1]))
    return out


# ---------------------------------------------------------------- 选择


def _scan_family(c: Candidate) -> tuple:
    """广扫专用去重键：不同类别即使底层 (维, 指标) 相同也各自保留。

    （原 family_key 为旧 Dashboard「comparison/share 二选一」设计，
    广扫需要两者同时作为证据，故按类别加前缀隔离。）
    """
    if c.type is ViewType.ranking:
        tag = "scan:bottom" if c.hint.get("order") == "asc" else "scan:top"
        return (tag, c.hint.get("dimension"), c.hint.get("metric"))
    mapping = {
        ViewType.overview: "scan:overview",
        ViewType.trend: "scan:trend",
        ViewType.comparison: "scan:comparison",
        ViewType.breakdown: "scan:share",
        ViewType.anomaly: "scan:anomaly",
        ViewType.relationship: "scan:rel",
        ViewType.profitability: "scan:ratio",
        ViewType.contribution: "scan:contrib",
        ViewType.rate_shift: "scan:rateshift",
    }
    tag = mapping[c.type]
    if c.type in (ViewType.comparison, ViewType.breakdown):
        return (tag, c.dimension_fields[0], c.metric_fields[0])
    if c.type in (ViewType.contribution, ViewType.rate_shift):
        return (tag, c.dimension_fields[0], tuple(c.metric_fields))
    if c.type is ViewType.profitability:
        return (tag, c.dimension_fields[0], tuple(c.metric_fields))
    if c.type is ViewType.relationship:
        return (tag, *c.fields)
    if c.metric_fields:
        return (tag, c.metric_fields[0])
    return (tag,)


def _rank_key(category: str, c: Candidate) -> tuple:
    """同类候选内的确定性排序。"""
    if category == "top_bottom":
        # Top/Bottom 成对：按 (指标, 维度, Top 先于 Bottom)
        return (
            c.hint.get("metric") or "",
            c.hint.get("dimension") or "",
            1 if c.hint.get("order") == "asc" else 0,
        )
    return (c.priority_group, c.metric_fields[0] if c.metric_fields else "")


def _select_scan(candidates: list[Candidate]) -> list[Candidate]:
    by_category: dict[str, list[Candidate]] = {}
    for c in candidates:
        category = _CATEGORY_OF_TYPE.get(c.type)
        if category is None:
            continue
        by_category.setdefault(category, []).append(c)

    selected: list[Candidate] = []
    used_families: set[tuple] = set()
    for category in _CATEGORY_ORDER:
        group = sorted(by_category.get(category, []), key=lambda c: _rank_key(category, c))
        taken = 0
        for c in group:
            if taken >= _CATEGORY_QUOTA[category]:
                break
            family = _scan_family(c)
            if family in used_families:
                continue
            if len(selected) >= MAX_SCAN_VIEWS:
                return selected
            used_families.add(family)
            selected.append(c)
            taken += 1
    return selected


# ---------------------------------------------------------------- 入口


def plan_scan(
    dictionary: DataDictionary,
    snapshot: pd.DataFrame | None,
    *,
    title: str = "自动广度扫描",
) -> AnalysisBundle:
    """纯函数：字典 + 快照 → 广扫 AnalysisBundle（不落盘，便于单测）。"""
    metrics = _ranked_metrics(dictionary)
    candidates = CandidateGenerator().generate(dictionary, snapshot)
    candidates = _augment(candidates, metrics, snapshot)
    chosen = _select_scan(candidates)
    views = [
        _to_view(i + 1, c).model_copy(update={"source": "rule.broad_scan"})
        for i, c in enumerate(chosen)
    ]
    dims = sorted(
        [f for f in dictionary.fields if not f.ignored],
        key=lambda f: -(f.cardinality or 0),
    )
    return AnalysisBundle(
        bundle_id=f"bundle_{uuid.uuid4().hex[:12]}",
        title=title,
        primary_metrics=[m.name for m in metrics[:2]],
        primary_dimensions=[f.name for f in dims[:3]],
        analysis_views=views,
    )
