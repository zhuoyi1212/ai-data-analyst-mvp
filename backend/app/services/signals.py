"""Signal Detection 与 Ranking（Task 3）。

两阶段：
    extract_signals  从 scan 视图的真实结果中提取八类信号（量级逐值引用
                     result.parquet / 引擎 summary，LLM 不参与判定）；
    rank_signals     多维评分（影响/集中度/新异性/支撑度，权重对齐 T09），
                     排序确定性，取 top K（默认 3）供 Diagnostic 深挖。

红线：信号的每个量级字段都必须能在 scan_view_ids 指向的视图中复算；
相关类信号不表述为因果。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.schemas.auto import Magnitude, ScoreBreakdown, Signal, SignalSet
from app.schemas.bundle import (
    AnalysisBundle,
    BundleExecutionResult,
    ViewType,
)
from app.schemas.dictionary import DataDictionary
from app.services.dashboard_insight import (
    MIN_ABS_R,
    MIN_GAP_PP,
    MIN_TREND_CHANGE,
    _load,
    _step_summary,
    _values,
)
from app.services.diagnostic_search import (
    DELTA_IMPACT_FULL,
    RATE_IMPACT_FULL_PP,
    W_CONCENTRATION,
    W_IMPACT,
    W_NOVELTY,
    W_SUPPORT,
    _score_concentration,
)
from app.services.storage import SessionStore

TOP_K = 3
MAX_NEGATIVE_PER_METRIC = 3

# 同分确定性次序
_TYPE_ORDER = {
    "negative_member": 0,
    "scale_profit": 1,
    "divergence": 2,
    "simpson": 3,
    "correlation": 4,
    "anomaly": 5,
    "decline": 6,
    "growth": 7,
}


@dataclass
class _Raw:
    type: str
    view_ids: list[str]
    metric: str
    direction: str
    magnitude: dict[str, Any]
    impact: float
    concentration: float
    novelty: float
    support: float = 0.9
    dimension: str | None = None
    member: str | None = None


# ---------------------------------------------------------------- 工具


def _consumable_views(bundle: AnalysisBundle, execution: BundleExecutionResult):
    ok = {r.view_id for r in execution.views if r.consumable}
    return {v.view_id: v for v in bundle.analysis_views if v.view_id in ok}


def _period(snapshot, view):
    from app.services.period_compare import period_comparison

    p = view.plan.steps[-1].params
    gran = p.granularity.value
    mode = {"month": "mom", "week": "wow", "year": "yoy"}.get(gran, "mom")
    return period_comparison(
        snapshot, p.date_column, view.metric_fields[0],
        p.func.value, mode,
    )


# ---------------------------------------------------------------- 提取


def extract_signals(
    session_id: str,
    store: SessionStore,
    bundle: AnalysisBundle,
    execution: BundleExecutionResult,
    dictionary: DataDictionary,
) -> list[Signal]:
    views = _consumable_views(bundle, execution)
    snapshot = store.load_snapshot(session_id)
    raws: list[_Raw] = []

    primary_dims = set(bundle.primary_dimensions)

    def novelty_for(dim: str | None) -> float:
        if dim is None:
            return 0.8
        return 0.4 if dim in primary_dims else 0.9

    # ------------------------------------------------ 1. 趋势增长/下滑
    trend_views = [v for v in views.values() if v.type is ViewType.trend]
    for tv in trend_views:
        try:
            pc = _period(snapshot, tv)
        except Exception:  # noqa: BLE001 —— 个别口径失败不影响信号阶段
            continue
        metric = tv.metric_fields[0]
        g = pc.get("growth_pct")
        if g is not None:
            if abs(float(g)) / 100.0 < MIN_TREND_CHANGE:
                continue
            growing = float(g) > 0
            raws.append(_Raw(
                type="growth" if growing else "decline",
                view_ids=[tv.view_id],
                metric=metric,
                direction="up" if growing else "down",
                magnitude={
                    "metric": metric,
                    "value": float(pc["current_value"]),
                    "compare_value": float(pc["previous_value"]),
                    "change_pct": float(g),
                },
                impact=min(1.0, abs(float(g)) / 100.0 / DELTA_IMPACT_FULL),
                concentration=0.0,
                novelty=novelty_for(None),
            ))
        elif "扭亏" in (pc.get("status") or "") or "由盈转亏" in (pc.get("status") or ""):
            crossing_down = "由盈转亏" in pc["status"]
            raws.append(_Raw(
                type="decline" if crossing_down else "growth",
                view_ids=[tv.view_id],
                metric=metric,
                direction="down" if crossing_down else "up",
                magnitude={
                    "metric": metric,
                    "value": float(pc["current_value"]),
                    "compare_value": float(pc["previous_value"]),
                },
                impact=0.8,
                concentration=0.0,
                novelty=novelty_for(None),
            ))

    # ------------------------------------------------ 2. 亏损成员
    neg_seen: dict[str, int] = {}
    member_views = [
        v for v in views.values()
        if v.type in (ViewType.comparison, ViewType.breakdown)
    ]
    for v in member_views:
        metric = v.metric_fields[0]
        if neg_seen.get(metric, 0) >= MAX_NEGATIVE_PER_METRIC:
            continue
        dim = v.dimension_fields[0]
        df = _load(store, session_id, v.view_id)
        for member, val in _values(df, dim).items():
            if val >= 0:
                continue
            if neg_seen.get(metric, 0) >= MAX_NEGATIVE_PER_METRIC:
                break
            neg_seen[metric] = neg_seen.get(metric, 0) + 1
            total_abs = float(df["value"].abs().sum())
            raws.append(_Raw(
                type="negative_member",
                view_ids=[v.view_id],
                metric=metric,
                dimension=dim,
                member=member,
                direction="negative",
                magnitude={"metric": metric, "value": float(val)},
                impact=min(1.0, abs(float(val)) / total_abs) if total_abs else 0.5,
                concentration=_score_concentration(df["value"], len(df)),
                novelty=novelty_for(dim),
            ))

    # ------------------------------------------------ 3-4. 派生指标类信号
    from app.services.derived_metrics import detect_derived_metrics

    derived = detect_derived_metrics(dictionary)
    if derived:
        rspec = derived[0]
        num, den = rspec.numerator, rspec.denominator

        # 3a. 量利份额背离：按维度配对 num/den 的结构视图
        by_dim_metric: dict[tuple[str, str], Any] = {}
        for v in member_views:
            if v.metric_fields[0] in (num, den):
                by_dim_metric[(v.dimension_fields[0], v.metric_fields[0])] = v
        for (dim, metric), v in list(by_dim_metric.items()):
            if metric != num:
                continue
            dv = by_dim_metric.get((dim, den))
            if dv is None:
                continue
            ndf = _load(store, session_id, v.view_id)
            ddf = _load(store, session_id, dv.view_id)
            # 直接由 value 归一（不依赖 share 列：总利润为负时份额语义失真）
            total_num = float(ndf["value"].sum())
            total_den = float(ddf["value"].sum())
            ns = (
                {str(k): float(x) / total_num
                 for k, x in zip(ndf[dim], ndf["value"])}
                if total_num != 0 else {}
            )
            ds = (
                {str(k): float(x) / total_den
                 for k, x in zip(ddf[dim], ddf["value"])}
                if total_den != 0 else {}
            )
            nvals = _values(ndf, dim)
            for member, s_den in ds.items():
                s_num = ns.get(member)
                gap = (s_num - s_den) * 100.0 if s_num is not None else None
                nval = nvals.get(member)
                if not ((gap is not None and gap <= -MIN_GAP_PP)
                        or (nval is not None and nval < 0)):
                    continue
                raws.append(_Raw(
                    type="divergence",
                    view_ids=[dv.view_id, v.view_id],
                    metric=num,
                    dimension=dim,
                    member=member,
                    direction="down",
                    magnitude={
                        "metric": num, "value": nval,
                        "gap_pp": round(gap, 4) if gap is not None else None,
                    },
                    impact=(
                        min(0.6 + abs(gap) / 40.0, 1.0) if gap is not None
                        else min(0.6 + abs(nval) / abs(total_num or 1.0), 1.0)
                    ),
                    concentration=_score_concentration(ndf["value"], len(ndf)),
                    novelty=novelty_for(dim),
                ))

        # 3b. 增收不增利
        if trend_views:
            date_col = trend_views[0].plan.steps[-1].params.date_column
            from app.services.period_compare import PeriodCompareError, period_comparison

            try:
                pc_s = period_comparison(snapshot, date_col, den, "sum", "mom")
                pc_p = period_comparison(snapshot, date_col, num, "sum", "mom")
            except PeriodCompareError:
                pc_s = pc_p = None
            if pc_s and pc_p:
                gs, gp = pc_s.get("growth_pct"), pc_p.get("growth_pct")
                p_status = pc_p.get("status") or ""
                scale_ok = gs is not None and float(gs) / 100.0 >= 0.05
                profit_ok = (
                    gp is not None and float(gp) / 100.0 <= -0.10
                ) or "由盈转亏" in p_status or (
                    "亏损扩大" in p_status and scale_ok
                )
                if scale_ok and profit_ok:
                    raws.append(_Raw(
                        type="scale_profit",
                        view_ids=[trend_views[0].view_id],
                        metric=num,
                        direction="mixed",
                        magnitude={
                            "metric": num,
                            "value": float(pc_p["current_value"]),
                            "compare_value": float(pc_p["previous_value"]),
                            "change_pct": float(gp) if gp is not None else None,
                        },
                        impact=0.9,
                        concentration=0.2,
                        novelty=novelty_for(None),
                    ))

    # ------------------------------------------------ 5. Simpson 结构悖论
    for v in views.values():
        if v.type is not ViewType.rate_shift:
            continue
        summ = _step_summary(execution, v.view_id)
        change_pp = summ.get("change_pp")
        within_pp = summ.get("within_effect_pp")
        if change_pp is None or within_pp is None:
            continue
        if float(change_pp) > -1.0 or float(within_pp) <= 0:
            continue
        dim = v.dimension_fields[0]
        raws.append(_Raw(
            type="simpson",
            view_ids=[v.view_id],
            metric=f"{v.metric_fields[0]}/{v.metric_fields[1]}",
            dimension=dim,
            direction="down",
            magnitude={
                "metric": f"{v.metric_fields[0]}/{v.metric_fields[1]}",
                "gap_pp": float(change_pp),
            },
            impact=min(1.0, abs(float(change_pp)) / RATE_IMPACT_FULL_PP),
            concentration=0.1,
            novelty=novelty_for(dim),
        ))

    # ------------------------------------------------ 6. 显著相关
    for v in views.values():
        if v.type is not ViewType.relationship:
            continue
        summ = _step_summary(execution, v.view_id)
        r, n = summ.get("coefficient"), summ.get("n")
        if r is None or abs(float(r)) < MIN_ABS_R:
            continue
        x, y = v.metric_fields[0], v.metric_fields[1]
        raws.append(_Raw(
            type="correlation",
            view_ids=[v.view_id],
            metric=f"{x}~{y}",
            direction="up" if float(r) > 0 else "down",
            magnitude={
                "metric": f"{x}~{y}", "coefficient": float(r), "n": int(n),
            },
            impact=abs(float(r)),
            concentration=0.0,
            novelty=novelty_for(None),
        ))

    # ------------------------------------------------ 7. 异常离群
    for v in views.values():
        if v.type is not ViewType.anomaly:
            continue
        summ = _step_summary(execution, v.view_id)
        count = summ.get("outlier_count")
        if count is None or int(count) == 0:
            continue
        metric = v.metric_fields[0]
        rate = float(summ.get("outlier_rate", 0.0))
        raws.append(_Raw(
            type="anomaly",
            view_ids=[v.view_id],
            metric=metric,
            direction="mixed",
            magnitude={"metric": metric, "value": float(count), "n": len(snapshot.index)},
            impact=min(0.5 + rate, 1.0),
            concentration=0.0,
            novelty=novelty_for(None),
        ))

    return rank_signals(raws)


# ---------------------------------------------------------------- 排序


def rank_signals(raws: list[_Raw], *, top_k: int = TOP_K) -> list[Signal]:
    weight_sum = W_IMPACT + W_CONCENTRATION + W_SUPPORT + W_NOVELTY

    def total(r: _Raw) -> float:
        score = (
            W_IMPACT * r.impact
            + W_CONCENTRATION * r.concentration
            + W_SUPPORT * r.support
            + W_NOVELTY * r.novelty
        ) / weight_sum
        return round(max(0.0, min(1.0, score)), 4)

    scored = [(total(r), r) for r in raws]
    scored.sort(key=lambda item: (
        -item[0], _TYPE_ORDER[item[1].type], item[1].metric, item[1].member or "",
    ))

    out: list[Signal] = []
    for i, (score, r) in enumerate(scored, start=1):
        out.append(Signal(
            signal_id=f"sig_{i:02d}",
            type=r.type,  # type: ignore[arg-type]
            scan_view_ids=r.view_ids,
            metric=r.metric,
            dimension=r.dimension,
            member=r.member,
            direction=r.direction,  # type: ignore[arg-type]
            magnitude=Magnitude(**r.magnitude),
            score=score,
            score_breakdown=ScoreBreakdown(
                impact=round(r.impact, 4),
                concentration=round(r.concentration, 4),
                novelty=round(r.novelty, 4),
                support=round(r.support, 4),
            ),
        ))
    return out


def build_signal_set(
    session_id: str, run_id: str, signals: list[Signal], *, top_k: int = TOP_K
) -> SignalSet:
    return SignalSet(
        session_id=session_id,
        run_id=run_id,
        signals=signals,
        top_signal_ids=[s.signal_id for s in signals[:top_k]],
    )
