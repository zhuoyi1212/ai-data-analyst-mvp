"""跨视图 Findings 检测器（重构 Phase 2，纯规则 + 阈值 + 中文模板）。

四类检测（数值全部来自 view result.parquet 或 probe，LLM 不参与）：
  1. divergence        同维度销售份额 vs 利润份额背离（缺口 ≥10pp 或利润为负）；
  2. risk.negative_member  利润为负的成员 + 下一层维度 drilldown probe（Top2 负贡献子成员）；
  3. relationship      |r| ≥ 0.3 的相关（负相关→risk），必附因果免责语；
  4. trend             末两期变化 ≥10% 的增长/下滑。

所有 Finding 四要素齐全：现象 / 位置 / 量化影响 / 关注建议。
LLM 增强点：FindingsDetector 协议后续可替换为 LLM 实现（同进同出），本阶段不实现。
"""
from __future__ import annotations

import re
from typing import Any, Protocol

import pandas as pd

from app.schemas.bundle import (
    AnalysisBundle,
    AnalysisView,
    BundleExecutionResult,
    ViewStatus,
    ViewType,
)
from app.schemas.common import AggFunc, FilterOperator, SortOrder
from app.schemas.dashboard import (
    DrillChild,
    DrilldownFilter,
    DrilldownSuggestion,
    Finding,
)
from app.schemas.dictionary import DataDictionary
from app.schemas.plan import (
    AnalysisPlan,
    FilterParams,
    FilterStep,
    GroupByParams,
    GroupByStep,
)
from app.services.derived_metrics import detect_derived_metrics
from app.services.storage import SessionStore

MIN_ABS_R = 0.30       # 相关显著阈值
MIN_GAP_PP = 10.0      # 利润份额−销售份额背离阈值（百分点）
MIN_TREND_CHANGE = 0.10
CAUSAL_NOTE = "数据关联不等于因果，建议结合业务动作做进一步验证。"
_RATE_DRIVER = re.compile(r"(折扣|discount|率|rate|price|成本占比)", re.I)

_DIST = (ViewType.comparison, ViewType.breakdown, ViewType.profitability)


# ----------------------------------------------------------------- 工具

def _fmt_num(v: float) -> str:
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:,.2f}"


def _fmt_pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def _success_views(
    bundle: AnalysisBundle, execution: BundleExecutionResult
) -> dict[str, AnalysisView]:
    """T04：与 KPI/图表同一门禁——只取 status=success 且结果可消费的 View。"""
    ok = {r.view_id for r in execution.views if r.consumable}
    return {v.view_id: v for v in bundle.analysis_views if v.view_id in ok}


def _load(store: SessionStore, sid: str, vid: str) -> pd.DataFrame:
    return pd.read_parquet(store.view_dir(sid, vid) / "result.parquet")


def _step_summary(execution: BundleExecutionResult, vid: str) -> dict[str, Any]:
    rec = next(r for r in execution.views if r.view_id == vid)
    return rec.steps[-1].summary if rec.steps else {}


def _shares(df: pd.DataFrame, dim: str) -> dict[str, float]:
    """成员→份额：share 列优先，否则由 value 归一（确定性比较口径）。"""
    if "share" in df.columns:
        return {str(k): float(v) for k, v in zip(df[dim], df["share"])}
    total = float(df["value"].sum())
    if total == 0:
        return {}
    return {str(k): float(v) / total for k, v in zip(df[dim], df["value"])}


def _values(df: pd.DataFrame, dim: str) -> dict[str, float]:
    return {str(k): float(v) for k, v in zip(df[dim], df["value"])}


# ----------------------------------------------------------------- 1. 量利背离

def _detect_divergence(
    sid: str, store: SessionStore, views: dict[str, AnalysisView],
    numerator: str, denominator: str, scope_tag: str,
) -> tuple[Finding, tuple] | None:
    # dim → {metric: (view, df)}
    # T07：背离回答的是 Seed 基准拆分这个业务问题——只看 presentation
    # breakdown 所在维度，不被额外维度的 computation 比较带偏。
    seed_dim = next(
        (v.dimension_fields[0] for v in views.values()
         if v.role == "presentation" and v.type is ViewType.breakdown
         and v.dimension_fields),
        None,
    )
    by_dim: dict[str, dict[str, tuple[AnalysisView, pd.DataFrame]]] = {}
    for v in views.values():
        if v.type not in (ViewType.comparison, ViewType.breakdown):
            continue
        if not v.dimension_fields or not v.metric_fields:
            continue
        if seed_dim is not None and v.dimension_fields[0] != seed_dim:
            continue
        metric = v.metric_fields[0]
        if metric not in (numerator, denominator):
            continue
        df = _load(store, sid, v.view_id)
        if v.dimension_fields[0] not in df.columns or "value" not in df.columns:
            continue
        by_dim.setdefault(v.dimension_fields[0], {})[metric] = (v, df)

    best = None  # (gap_pp, dim, member, num_share, den_share, negative_members, ids)
    for dim, pair in by_dim.items():
        if numerator not in pair or denominator not in pair:
            continue
        (nv, ndf), (dv, ddf) = pair[numerator], pair[denominator]
        ns, ds = _shares(ndf, dim), _shares(ddf, dim)
        nvals = _values(ndf, dim)
        negatives = {m: val for m, val in nvals.items() if val < 0}
        for member, s_den in ds.items():
            s_num = ns.get(member)
            if s_num is None:
                continue
            gap = (s_num - s_den) * 100
            if gap <= -MIN_GAP_PP or member in negatives:
                key = (gap, member)
                if best is None or key < (best[0], best[2]):
                    best = (
                        gap, dim, member, s_num, s_den,
                        sorted(negatives.items(), key=lambda x: x[1]),
                        [dv.view_id, nv.view_id],
                    )
    if best is None:
        return None
    gap, dim, member, s_num, s_den, negatives, evidence = best
    neg_text = (
        f"；其中 {len(negatives)} 个成员利润为负"
        f"（最差「{negatives[0][0]}」{_fmt_num(negatives[0][1])}）"
        if negatives else ""
    )
    importance = "high" if negatives else "medium"
    finding = Finding(
        finding_id="",  # 由 assemble 统一编号
        title=f"「{dim}」量利结构背离：{member} 收入占比高但利润贡献低",
        summary=(
            f"现象：按「{dim}」看，{numerator} 与 {denominator} 的份额出现明显背离。"
            f"位置：成员「{member}」占{denominator}的 {_fmt_pct(s_den)}，"
            f"但只贡献 {numerator} 的 {_fmt_pct(s_num)}，缺口 {abs(gap):.1f} 个百分点{neg_text}。"
            f"建议：优先核查该成员的折扣、成本与费用结构，判断是战略性投入还是盈利漏损。"
        ),
        type="structure",
        evidence_view_ids=evidence,
        importance=importance,
    )
    # T07 去重键（metric, scope, period, question）：与同成员亏损风险合并
    dedup_key = (numerator, scope_tag, "", f"profit_problem:{dim}:{member}")
    return finding, dedup_key


# ----------------------------------------------------------------- 2. 负成员风险 + 下钻

# 下钻子维度的层级语义优先级（Category → Sub-Category/Product，而非 Region 等平级维度）
_CHILD_HIER = re.compile(r"(子|sub|二级|下级|下一层)", re.I)
_CHILD_PRODUCT = re.compile(r"(product|商品|品类|sku|item|货品|产品)", re.I)


def _child_rank(name: str) -> int:
    if _CHILD_HIER.search(name):
        return 0
    if _CHILD_PRODUCT.search(name):
        return 1
    return 2


def _child_dimension(
    dictionary: DataDictionary, parent_dim: str, date_cols: set[str]
) -> str | None:
    parent = next((f for f in dictionary.fields if f.name == parent_dim), None)
    parent_card = parent.cardinality if parent else None
    cands = [
        f for f in dictionary.fields
        if f.name != parent_dim and f.name not in date_cols
        and 1 < (f.cardinality or 0) <= 30
        and (parent_card is None or f.cardinality > parent_card)
    ]
    if not cands:
        return None
    # 先比层级语义（子级/商品类优先），再取基数最接近父级的维度
    return sorted(
        cands,
        key=lambda f: (_child_rank(f.name), f.cardinality or 999, f.name),
    )[0].name


def _detect_negative_risk(
    sid: str, store: SessionStore,
    views: dict[str, AnalysisView], dictionary: DataDictionary,
    numerator: str, runner, scope_tag: str,
) -> tuple[Finding, tuple] | None:
    # 选利润为负最严重的成员（只用原始指标口径的 comparison/breakdown 视图；
    # profitability 的 value 是派生比率，不能当金额引用）
    worst = None  # (value, dim, member, view_id)
    order = (ViewType.comparison, ViewType.breakdown)
    ranked = sorted(
        views.values(),
        key=lambda v: order.index(v.type) if v.type in order else len(order),
    )
    for v in ranked:
        if v.type not in order or not v.metric_fields:
            continue
        if v.metric_fields[0] != numerator:
            continue
        dim = v.dimension_fields[0]
        df = _load(store, sid, v.view_id)
        if dim not in df.columns or "value" not in df.columns:
            continue
        for member, val in _values(df, dim).items():
            if val < 0 and (worst is None or val < worst[0]):
                worst = (val, dim, member, v.view_id)
    if worst is None:
        return None
    value, dim, member, evidence_vid = worst

    drilldown: DrilldownSuggestion | None = None
    date_cols = set(dictionary.date_fields())
    child = _child_dimension(dictionary, dim, date_cols)
    if child is not None:
        plan = AnalysisPlan(
            question=f"「{dim}」=「{member}」时，各「{child}」的 {numerator} 是多少？",
            data_scope=f"筛选 {dim} == {member} 后的全部快照数据",
            steps=[
                FilterStep(
                    step_id="filter_member",
                    op="filter",
                    params=FilterParams(
                        column=dim, operator=FilterOperator.eq, value=member
                    ),
                    description=f"筛选「{dim}」等于「{member}」",
                ),
                GroupByStep(
                    step_id="group_child",
                    op="group_by",
                    params=GroupByParams(
                        dimension=child, metric=numerator,
                        func=AggFunc.sum, order=SortOrder.asc, limit=10,
                    ),
                    description=f"按「{child}」汇总「{numerator}」（升序，定位亏损子成员）",
                ),
            ],
            expected_shape="筛选后按子维度分组的多行结果",
        )
        probe = runner.run(f"probe_drill_{dim}_{member}".lower(), plan)
        if probe is not None and child in probe.df.columns:
            neg_kids = [
                (str(k), float(v))
                for k, v in zip(probe.df[child], probe.df["value"])
                if pd.notna(v) and v < 0
            ][:2]
            drilldown = DrilldownSuggestion(
                filters=[DrilldownFilter(column=dim, value=member)],
                child_dimension=child,
                top_negative_children=[
                    DrillChild(name=n, value=v) for n, v in neg_kids
                ],
            )

    if drilldown and drilldown.top_negative_children:
        kids = "、".join(
            f"「{c.name}」{_fmt_num(c.value)}"
            for c in drilldown.top_negative_children
        )
        location = (
            f"位置：「{dim}」成员「{member}」整体 {numerator} 为 {_fmt_num(value)}；"
            f"下钻到「{drilldown.child_dimension}」，负贡献最大的是 {kids}。"
        )
        suggestion = "建议针对上述子成员先做折扣/成本专项核查，再决定收缩或整改。"
    else:
        location = f"位置：「{dim}」成员「{member}」整体 {numerator} 为 {_fmt_num(value)}。"
        suggestion = "建议下钻到更细维度（子品类/区域/客户）定位亏损来源。"
    finding = Finding(
        finding_id="",
        title=f"亏损风险：「{member}」{numerator} 为负",
        summary=(
            f"现象：部分维度成员处于亏损状态。{location}"
            f"量化影响：该成员每单位收入都在拉低整体盈利。{suggestion}"
        ),
        type="risk",
        evidence_view_ids=[evidence_vid],
        importance="high",
        drilldown=drilldown,
    )
    # T07：与同成员量利背离合并（同一业务问题）
    dedup_key = (numerator, scope_tag, "", f"profit_problem:{dim}:{member}")
    return finding, dedup_key


# ----------------------------------------------------------------- 3. 相关性

def _detect_relationship(
    sid: str, store: SessionStore,
    views: dict[str, AnalysisView], execution: BundleExecutionResult,
    scope_tag: str,
) -> list[tuple[Finding, tuple]]:
    out: list[tuple[Finding, tuple]] = []
    for v in views.values():
        if v.type is not ViewType.relationship:
            continue
        summary = _step_summary(execution, v.view_id)
        r = summary.get("coefficient")
        n = summary.get("n")
        if r is None or abs(float(r)) < MIN_ABS_R:
            continue
        r = float(r)
        x, y = v.metric_fields[0], v.metric_fields[1]
        # 负相关时把率/折扣类指标作为驱动因素（x），文案方向才可读
        if r < 0 and _RATE_DRIVER.search(x) is None and _RATE_DRIVER.search(y):
            x, y = y, x
        if r < 0:
            f = Finding(
                finding_id="",
                title=f"负相关风险：「{x}」越高时「{y}」越低（r={r:.2f}）",
                summary=(
                    f"现象：「{x}」与「{y}」呈较强负相关（Pearson r={r:.2f}，n={n}）。"
                    f"位置：基于全部 {n} 行快照的指标间关系，无额外筛选。"
                    f"量化影响：{x} 每上升一个区间，{y} 倾向于明显走低，可能侵蚀盈利。"
                    f"建议：复核高{x}区间的定价/促销策略，必要时设置阈值管控。{CAUSAL_NOTE}"
                ),
                type="risk",
                evidence_view_ids=[v.view_id],
                importance="high",
            )
        else:
            f = Finding(
                finding_id="",
                title=f"指标关联：「{x}」与「{y}」正相关（r={r:.2f}）",
                summary=(
                    f"现象：「{x}」与「{y}」呈较强正相关（Pearson r={r:.2f}，n={n}）。"
                    f"位置：基于全部 {n} 行快照的指标间关系，无额外筛选。"
                    f"量化影响：两者联动明显，可作为联合监控指标。"
                    f"建议：在经营看板中并列跟踪，验证联动是否稳定。{CAUSAL_NOTE}"
                ),
                type="relationship",
                evidence_view_ids=[v.view_id],
                importance="medium",
            )
        key = (f"{x}~{y}", scope_tag, "", f"relationship:{x}:{y}")
        out.append((f, key))
    return out


# ----------------------------------------------------------------- 4. 趋势

_GRAN_TO_MODE = {"month": "mom", "week": "wow", "year": "yoy"}


def _metric_is_rate(dictionary: DataDictionary, metric: str) -> bool:
    from app.services.metric_spec import effective_metric_spec
    fp = next((f for f in dictionary.fields if f.name == metric), None)
    spec = effective_metric_spec(fp) if fp else None
    return bool(spec and not spec.additive and (spec.unit == "%" or spec.denominator))


def _detect_trends(
    sid: str, store: SessionStore,
    views: dict[str, AnalysisView], dictionary: DataDictionary,
    scope_tag: str,
) -> list[tuple[Finding, tuple]]:
    """遍历 Seed 中全部 presentation 趋势视图（m0、m1 各产各的 Finding）。

    T07 修复：旧实现 next() 只取第一个趋势，第二核心指标（如 Profit）
    明显下滑时 Findings 层零信号。
    """
    trend_views = [
        v for v in views.values()
        if v.type is ViewType.trend and v.role == "presentation"
        and v.metric_fields
    ]
    out: list[tuple[Finding, tuple]] = []
    for tv in trend_views:
        item = _detect_one_trend(tv, sid, store, dictionary, scope_tag)
        if item is not None:
            out.append(item)
    return out


def _detect_one_trend(
    tv: AnalysisView,
    sid: str, store: SessionStore,
    dictionary: DataDictionary, scope_tag: str,
) -> tuple[Finding, tuple] | None:
    params = tv.plan.steps[-1].params
    date_col = params.date_column
    gran = params.granularity.value
    metric = tv.metric_fields[0]
    mode = _GRAN_TO_MODE.get(gran, "mom")
    period_name = {"month": "月", "week": "周", "day": "日",
                   "quarter": "季", "year": "年"}.get(gran, "期")

    # T05：直接在快照上走 period_comparison 等长窗口口径，
    # 不拿趋势图最后两个可能残缺的聚合桶直接相除（反例 5/6）。
    from app.services.period_compare import PeriodCompareError, period_comparison
    snapshot = store.load_snapshot(sid)
    try:
        pc = period_comparison(
            snapshot, date_col, metric, params.func.value, mode,
            is_rate=_metric_is_rate(dictionary, metric),
        )
    except PeriodCompareError:
        return None
    if pc is None:
        return None

    prev, last = pc["previous_value"], pc["current_value"]
    partial_note = (
        "（当期未结束，按截至日等长窗口比较）" if pc["completeness"] != "complete" else ""
    )
    if pc["growth_pct"] is None:
        status = pc.get("status") or ""
        if pc["delta"] is None or (
            "扭亏" not in status and "亏损" not in status
        ):
            return None
        growing = pc["delta"] > 0
        title = f"{status}：{metric} 变化 {pc['delta']:+,.0f}"
        change_text = f"绝对变化 {pc['delta']:+,.0f}（负基数不展示增长率）"
    else:
        growth = pc["growth_pct"] / 100.0
        if abs(growth) < MIN_TREND_CHANGE:
            return None
        growing = growth > 0
        title = (
            f"{'增长' if growing else '下滑'}信号：{metric} "
            f"环比{'增长' if growing else '下滑'} {abs(growth) * 100:.1f}%"
        )
        change_text = f"变化 {abs(growth) * 100:.1f}%"
    finding = Finding(
        finding_id="",
        title=title,
        summary=(
            f"现象：{metric} 最近一个{period_name}出现明显{'增长' if growing else '下滑'}。"
            f"位置：{pc['compare_label']} → {pc['current_label']}（{pc['comparison']}口径）。"
            f"量化影响：{_fmt_num(float(prev))} → {_fmt_num(float(last))}，"
            f"{change_text}{partial_note}。"
            f"建议：{'复盘增长来源并判断是否可持续' if growing else '排查当期促销、渠道或供给因素，确认是否趋势性下滑'}。"
        ),
        type="growth" if growing else "decline",
        evidence_view_ids=[tv.view_id],
        importance="medium",
    )
    key = (metric, scope_tag, mode, f"trend:{metric}:{mode}")
    return finding, key


# ----------------------------------------------------------------- 5. 异常

def _detect_anomaly(
    sid: str, store: SessionStore,
    views: dict[str, AnalysisView], execution: BundleExecutionResult,
    scope_tag: str,
) -> tuple[Finding, tuple] | None:
    """anomaly 证据任务存在离群点才产出 Finding；无异常默认不凑。"""
    av = next((v for v in views.values() if v.type is ViewType.anomaly), None)
    if av is None or not av.metric_fields:
        return None
    metric = av.metric_fields[0]
    summary = _step_summary(execution, av.view_id)
    count = summary.get("outlier_count")
    if count is None or int(count) == 0:
        return None
    count = int(count)
    rate = float(summary.get("outlier_rate", 0.0))
    lo = summary.get("lower_bound")
    hi = summary.get("upper_bound")
    bounds = f"，判定区间 [{_fmt_num(float(lo))}, {_fmt_num(float(hi))}]" if lo is not None else ""
    finding = Finding(
        finding_id="",
        title=f"异常信号：「{metric}」发现 {count} 个离群值（占 {rate * 100:.1f}%）",
        summary=(
            f"现象：IQR 规则在「{metric}」上标记了离群值{bounds}。"
            f"位置：全部快照中 {count} 行被标记，离群率 {rate * 100:.1f}%。"
            f"量化影响：这些极端值可能放大或拉低聚合结果，影响趋势判断。"
            f"建议：按离群优先排序的明细逐条核查，先排除录入错误，再判断是否真实业务事件。"
        ),
        type="anomaly",
        evidence_view_ids=[av.view_id],
        importance="high" if rate >= 0.05 else "medium",
    )
    key = (metric, scope_tag, "", f"anomaly:{metric}")
    return finding, key


# ----------------------------------------------------------------- 协议与编排

class FindingsDetector(Protocol):
    """LLM 增强替换点：未来 LLM 检测器实现同签名即可（本阶段不实现）。"""

    def detect(
        self,
        session_id: str,
        store: SessionStore,
        bundle: AnalysisBundle,
        execution: BundleExecutionResult,
        dictionary: DataDictionary,
        runner,
    ) -> list[Finding]: ...


def detect_findings(
    session_id: str,
    store: SessionStore,
    bundle: AnalysisBundle,
    execution: BundleExecutionResult,
    dictionary: DataDictionary,
    runner,
    *,
    scope_tag: str = "",
) -> list[Finding]:
    views = _success_views(bundle, execution)
    specs = detect_derived_metrics(dictionary)
    numerator = specs[0].numerator if specs else None
    denominator = specs[0].denominator if specs else None

    raw: list[tuple[Finding, tuple]] = []
    if numerator and denominator:
        d = _detect_divergence(
            session_id, store, views, numerator, denominator, scope_tag
        )
        if d:
            raw.append(d)
        risk = _detect_negative_risk(
            session_id, store, views, dictionary, numerator, runner, scope_tag
        )
        if risk:
            raw.append(risk)
    raw.extend(_detect_relationship(
        session_id, store, views, execution, scope_tag
    ))
    raw.extend(_detect_trends(session_id, store, views, dictionary, scope_tag))
    a = _detect_anomaly(session_id, store, views, execution, scope_tag)
    if a:
        raw.append(a)

    merged = merge_same_question(raw)
    for i, f in enumerate(merged, start=1):
        f.finding_id = f"finding_{i:02d}"
    return merged


# ----------------------------------------------------------------- 同问题合并

_IMPORTANCE_ORDER = {"high": 0, "medium": 1, "low": 2}


def merge_same_question(
    raw: list[tuple[Finding, tuple]],
) -> list[Finding]:
    """T07：去重键 (metric, scope, period, question) 相同的 Findings 合并。

    - 证据视图 ID 取并集（同一问题的多条证据全部保留可下钻）；
    - 重要性取最高，标题/摘要以最高重要性 Finding 为主，
      其余以「另见」标题补充，不丢弃信息。
    """
    groups: dict[tuple, list[Finding]] = {}
    order: list[tuple] = []
    for finding, key in raw:
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(finding)

    out: list[Finding] = []
    for key in order:
        items = sorted(groups[key], key=lambda f: _IMPORTANCE_ORDER[f.importance])
        primary = items[0]
        if len(items) == 1:
            out.append(primary)
            continue
        evidence = sorted({
            vid for f in items for vid in f.evidence_view_ids
        })
        extras = [f"另见「{f.title}」。" for f in items[1:]]
        merged = primary.model_copy(update={
            "evidence_view_ids": evidence,
            "summary": primary.summary + "".join(extras),
            "drilldown": primary.drilldown or next(
                (f.drilldown for f in items[1:] if f.drilldown), None
            ),
        })
        out.append(merged)
    return out
