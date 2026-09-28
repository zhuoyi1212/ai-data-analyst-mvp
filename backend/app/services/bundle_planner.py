"""AnalysisBundle 规划器（重构 Phase 1，规则版；架构为 LLM 替换预留）。

三阶段流水线：
    Candidate Generation（穷举所有「字段条件 × 分析类型」候选，允许冗余）
        → Selection（去重 + 多样性选择；Phase 1 用规则，Phase 2 可替换为 LLM）
        → AnalysisBundle（4-8 个高价值 View，每个内嵌既有 11 算子 AnalysisPlan）

多样性目标（用户明确要求）：
    1. metric diversity    —— 不只围绕一个主指标；存在 Sales/Profit 等多个高价值指标时
                              同时产出对照视角，为跨 View 洞察打基础；
    2. dimension diversity —— comparison / breakdown / ranking 尽量落在不同维度；
    3. analysis type diversity —— 7 种视角按字段条件启用；
    4. information redundancy —— 同一 (dimension, metric) 不允许仅换 share/group_by/top_n
                              就重复占据 Dashboard。

LLM 替换点：RuleBundleSelector 实现 BundleSelector 协议；未来新增 LLMBundleSelector
（产出同样的 AnalysisView 契约）即可无缝替换，Candidate 结构与去重键保持不变。
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace
from typing import Any, Protocol

import pandas as pd

from app.schemas.bundle import (
    VIEW_CHART,
    AnalysisBundle,
    AnalysisView,
    ViewType,
)
from app.schemas.common import AggFunc, ChartType
from app.schemas.dictionary import DataDictionary, FieldProfile, MetricSpec
from app.schemas.plan import AnalysisPlan
from app.services.derived_metrics import detect_derived_metrics
from app.services.metric_spec import effective_metric_spec
from app.services.offline_fallback import (
    _granularity,
    _high_cardinality_dimension,
    _metrics,
    _ranked_dimensions,
    build_plan,
)

# ---------------------------------------------------------------- 指标价值评估

# P&L 核心指标（可加总、业务价值最高）——注意 profit 也在其中，保证 Sales/Profit 并列
_PNL_HINT = re.compile(
    r"(利润|profit|净利|毛利|销售额|销售收入|收入|营收|金额|总额|"
    r"amount|revenue|sales|spend|花费|费用)",
    re.I,
)
_QTY_HINT = re.compile(r"(销量|数量|件数|订单数|qty|quantity|orders?|count|cnt)", re.I)
# 适合均值的率/单值指标（不可加总）
_RATE_HINT = re.compile(
    r"(折扣|discount|率$|比率|单价|price|时长|小时|hours?|分钟|minutes?|"
    r"score|评分|均值|平均|avg|dau|留存|转化)",
    re.I,
)


def _metric_score(name: str) -> int:
    """指标业务价值分：P&L 类 4，量级类 3，其余 1。"""
    if _PNL_HINT.search(name):
        return 4
    if _QTY_HINT.search(name):
        return 3
    return 1


def _metric_func(name: str) -> AggFunc:
    """率/单价/时长类取均值，其余可加总。确定性，禁止 LLM 参与。

    历史命名启发保留给单问题链路；Bundle 链路统一走 _spec_func（MetricSpec）。
    """
    return AggFunc.mean if _RATE_HINT.search(name) else AggFunc.sum


# MetricSpec.aggregation → 算子 AggFunc；"none"（口径待澄清）不自动聚合
_SPEC_AGG: dict[str, AggFunc] = {
    "sum": AggFunc.sum,
    "mean": AggFunc.mean,
    "median": AggFunc.median,
    "count": AggFunc.count,
    "count_distinct": AggFunc.count_distinct,
    "snapshot_last": AggFunc.last,
}
_FUNC_ZH = {
    AggFunc.sum: "总和", AggFunc.mean: "平均值", AggFunc.median: "中位数",
    AggFunc.count: "计数", AggFunc.count_distinct: "去重计数",
    AggFunc.min: "最小值", AggFunc.max: "最大值", AggFunc.last: "期末值",
}


def _spec_func(spec: MetricSpec | None) -> AggFunc | None:
    if spec is None:
        return AggFunc.sum  # 兜底：未识别指标维持历史口径
    return _SPEC_AGG.get(spec.aggregation)


def _ranked_metrics(dictionary: DataDictionary) -> list[FieldProfile]:
    return sorted(_metrics(dictionary), key=lambda m: -_metric_score(m.name))


# ---------------------------------------------------------------- 候选模型

@dataclass(frozen=True)
class Candidate:
    """一个待选分析视角（候选可能冗余，由 Selection 去重）。"""

    type: ViewType
    title: str
    question: str
    op: str  # 11 算子白名单中的算子名
    hint: dict[str, Any]
    fields: list[str]
    metric_fields: list[str]
    dimension_fields: list[str]
    family_key: tuple  # 信息冗余去重键
    reason: str
    priority_group: int  # 选择阶段的优先级（小者优先）


@dataclass
class SelectionContext:
    """选择阶段上下文（规则与未来 LLM 选择器共用）。"""

    metrics: list[FieldProfile]
    dimensions: list[FieldProfile]
    snapshot_rows: int
    max_views: int = 8


# ---------------------------------------------------------------- 候选生成

_AGG_LABEL = {AggFunc.sum: "总和", AggFunc.mean: "平均值"}


class CandidateGenerator:
    """穷举候选：宁可多生成（含冗余），选择权交给 Selector。"""

    MIN_ROWS = 30  # 相关/离群需要的最小样本

    def generate(
        self, dictionary: DataDictionary, df: pd.DataFrame | None
    ) -> list[Candidate]:
        metrics = _ranked_metrics(dictionary)
        specs = {m.name: effective_metric_spec(m) for m in metrics}
        dims = _ranked_dimensions(dictionary)
        date_fields = dictionary.date_fields()
        date_col = date_fields[0] if date_fields else None
        gran = _granularity(df, date_col) if date_col else None
        rows = len(df.index) if df is not None else 0

        out: list[Candidate] = []

        def _agg(m: FieldProfile) -> AggFunc | None:
            return _spec_func(specs.get(m.name))

        # 1) overview：每个高价值指标各一个候选（多指标对照的基础）
        #    口径为 none（关键歧义待澄清）的指标不生成自动聚合视角
        for m in metrics[:3]:
            func = _agg(m)
            if func is None:
                continue
            zh = _FUNC_ZH.get(func, func.value)
            out.append(Candidate(
                type=ViewType.overview,
                title=f"「{m.name}」总览",
                question=f"全部记录的「{m.name}」{zh}是多少？",
                op="aggregate",
                hint={"column": m.name, "func": func.value},
                fields=[m.name],
                metric_fields=[m.name],
                dimension_fields=[],
                family_key=("overview", m.name),
                reason=f"{m.name} 是识别出的高价值指标（价值分 {_metric_score(m.name)}，口径 {zh}）",
                priority_group=0,
            ))

        # 2) trend：主指标必有趋势（即使是 DAU/库存/时长等非金额类）；
        #    第二趋势仅给第二个 P&L 指标（多指标趋势对照）
        if date_col and gran is not None and metrics:
            primary = metrics[0]
            pnl_extra = [m for m in metrics[1:] if _metric_score(m.name) >= 4][:1]
            for i, m in enumerate([primary] + pnl_extra):
                func = _agg(m)
                if func is None:
                    continue
                out.append(Candidate(
                    type=ViewType.trend,
                    title=f"「{m.name}」按{gran.value}趋势",
                    question=f"「{m.name}」按{gran.value}的变化趋势如何？",
                    op="time_series",
                    hint={
                        "date_column": date_col,
                        "granularity": gran.value,
                        "metric": m.name,
                        "func": func.value,
                    },
                    fields=[date_col, m.name],
                    metric_fields=[m.name],
                    dimension_fields=[date_col],
                    family_key=("trend", m.name),
                    reason=f"存在日期字段「{date_col}」，{rows} 行覆盖足够，按{gran.value}聚合",
                    priority_group=1 if i == 0 else 6,
                ))

        # 3) 维度分布族：comparison(group_by) / breakdown(share) / ranking(top_n)
        #    低中基数维度（≤30）→ 对比与占比；任意维度 → Top N（高基数更优）
        #    T03 铁律：非可加指标（率/评分/期末库存）不生成份额视角，
        #    含合法负值组成的份额禁用饼图（share_bar 代替）。
        low_dims = [d for d in dims if 1 < (d.cardinality or 0) <= 30][:3]
        hc = _high_cardinality_dimension(dictionary, exclude=set())
        top_metrics = [m for m in metrics[:2] if _agg(m) is not None]
        for m in top_metrics:
            func = _agg(m)
            spec = specs.get(m.name)
            additive = bool(spec and spec.additive)
            for d in low_dims:
                out.append(Candidate(
                    type=ViewType.comparison,
                    title=f"各「{d.name}」的「{m.name}」对比",
                    question=f"不同「{d.name}」之间，「{m.name}」差异有多大？",
                    op="group_by",
                    hint={"dimension": d.name, "metric": m.name, "func": func.value},
                    fields=[d.name, m.name],
                    metric_fields=[m.name],
                    dimension_fields=[d.name],
                    family_key=("dist", d.name, m.name),
                    reason=f"「{d.name}」有 {d.cardinality} 个成员，适合分组对比",
                    priority_group=2,
                ))
                if additive:
                    share_hint = {
                        "dimension": d.name, "metric": m.name, "func": func.value,
                    }
                    # 负值组成（利润等）不用饼图：份额条形图展示正负贡献
                    if spec and spec.allowed_negative:
                        share_hint["chart_override"] = ChartType.share_bar.value
                    out.append(Candidate(
                        type=ViewType.breakdown,
                        title=f"「{m.name}」按「{d.name}」结构",
                        question=f"「{m.name}」在各「{d.name}」之间的占比结构如何？",
                        op="share",
                        hint=share_hint,
                        fields=[d.name, m.name],
                        metric_fields=[m.name],
                        dimension_fields=[d.name],
                        family_key=("dist", d.name, m.name),  # 与对比同族：同维同指标只留一个
                        reason=(
                            f"「{d.name}」{d.cardinality} 个成员，占比可读"
                            + ("（含可能的负值，用条形图展示正负贡献，不用饼图）"
                               if spec and spec.allowed_negative else "")
                        ),
                        priority_group=2,
                    ))
            rank_dim = hc or (low_dims[2] if len(low_dims) > 2 else None)
            if rank_dim is not None:
                out.append(Candidate(
                    type=ViewType.ranking,
                    title=f"「{rank_dim.name}」「{m.name}」Top 5",
                    question=f"「{m.name}」最高的 5 个「{rank_dim.name}」是谁？",
                    op="top_n",
                    hint={
                        "dimension": rank_dim.name, "metric": m.name,
                        "func": func.value, "n": 5,
                    },
                    fields=[rank_dim.name, m.name],
                    metric_fields=[m.name],
                    dimension_fields=[rank_dim.name],
                    family_key=("dist", rank_dim.name, m.name),
                    reason=(
                        f"「{rank_dim.name}」基数 {rank_dim.cardinality}，适合 Top N 排名"
                        if rank_dim is hc
                        else f"无高基数维度时，用「{rank_dim.name}」做排名补充"
                    ),
                    priority_group=2,
                ))

        # 3.5) profitability：确定性派生比率（如利润率 = ΣProfit/ΣSales）。
        #      公式来自规则注册表，数值由 derive_ratio 算子计算，LLM 不参与。
        #      锚点为低基数结构维度（候选全生成，锚点选择在 Selector）。
        for spec in detect_derived_metrics(dictionary):
            for d in low_dims:
                out.append(Candidate(
                    type=ViewType.profitability,
                    title=f"各「{d.name}」的「{spec.label}」",
                    question=(
                        f"各「{d.name}」的「{spec.label}」"
                        f"（Σ{spec.numerator}÷Σ{spec.denominator}）如何？"
                    ),
                    op="derive_ratio",
                    hint={
                        "numerator": spec.numerator,
                        "denominator": spec.denominator,
                        "dimension": d.name,
                    },
                    fields=[spec.numerator, spec.denominator, d.name],
                    metric_fields=[spec.numerator, spec.denominator],
                    dimension_fields=[d.name],
                    family_key=("ratio", d.name, spec.key),
                    reason=(
                        f"规则探测到确定性派生指标「{spec.label}」"
                        f"=Σ{spec.numerator}/Σ{spec.denominator}（先聚合后相除），"
                        f"按「{d.name}」诊断盈利结构，能识别收入大但亏损的成员"
                    ),
                    priority_group=3,
                ))

        # 4) relationship：优先字典探测到的相关对；否则在快照上确定性计算
        #    （含率类指标对优先、|r| 显著才采用）；样本不足不生成
        pair, pair_computed = self._best_correlation_pair(dictionary, metrics, df)
        if pair and rows >= self.MIN_ROWS:
            x, y = pair
            out.append(Candidate(
                type=ViewType.relationship,
                title=f"「{x}」与「{y}」的关系",
                question=f"「{x}」与「{y}」之间存在怎样的相关关系？",
                op="correlation",
                hint={"x_column": x, "y_column": y},
                fields=[x, y],
                metric_fields=[x, y],
                dimension_fields=[],
                family_key=("rel", x, y),
                reason=(
                    "快照规则计算中该指标对相关性最显著（含率/折扣类指标优先），值得关注"
                    if pair_computed
                    else "字典规则已探测到该指标对的相关性（或它们是两个主要数值指标）"
                ),
                priority_group=5,
            ))

        # 5) anomaly：主指标离群（样本足够才生成）
        if metrics and rows >= self.MIN_ROWS:
            m = metrics[0]
            out.append(Candidate(
                type=ViewType.anomaly,
                title=f"「{m.name}」离群检测",
                question=f"「{m.name}」是否存在需要关注的异常值？",
                op="outlier_flag",
                hint={"column": m.name},
                fields=[m.name],
                metric_fields=[m.name],
                dimension_fields=[],
                family_key=("anom", m.name),
                reason=f"{rows} 行样本下用 IQR 1.5 倍规则识别「{m.name}」离群",
                priority_group=4,
            ))

        return out

    # 快照直接计算时，|r| 低于该值视为无显著关系，回退为前两个指标
    MIN_ABS_R = 0.15

    @staticmethod
    def _best_correlation_pair(
        dictionary: DataDictionary,
        metrics: list[FieldProfile],
        df: pd.DataFrame | None,
    ) -> tuple[tuple[str, str] | None, bool]:
        """返回 ((x, y), computed)；弱信号/无证据时返回 None（反例 4：r=0 不占位）。"""
        rels = [
            r for r in dictionary.relations
            if r.type == "correlation" and len(r.columns) >= 2
            and r.coefficient is not None
            and abs(r.coefficient) >= CandidateGenerator.MIN_ABS_R
        ]
        if rels:
            # 含率/折扣类指标的对优先（如 折扣 vs 利润，负相关更有业务意义），
            # 其次取绝对相关系数最大者
            def rel_key(r):
                has_rate = 1 if (
                    _RATE_HINT.search(r.columns[0]) or _RATE_HINT.search(r.columns[1])
                ) else 0
                return (has_rate, abs(r.coefficient or 0.0))
            best = max(rels, key=rel_key)
            return (best.columns[0], best.columns[1]), False

        if len(metrics) < 2 or df is None:
            return None, False

        # 字典未给关系：在快照上确定性计算（最多前 5 个指标，纯 pandas 无 LLM）
        cols = [m.name for m in metrics[:5] if m.name in df.columns]
        if len(cols) < 2:
            return None, False
        nums = df[cols].apply(pd.to_numeric, errors="coerce")
        corr = nums.corr(method="pearson")
        best: tuple[str, str] | None = None
        best_key = (-1, -1.0)
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                r = corr.iloc[i, j]
                if pd.isna(r):
                    continue
                has_rate = 1 if (_RATE_HINT.search(cols[i])
                                 or _RATE_HINT.search(cols[j])) else 0
                key = (has_rate, abs(float(r)))
                if key > best_key:
                    best_key = key
                    best = (cols[i], cols[j])
        # |r| 不显著就不生成关系视角——阴性结果不占默认 Dashboard 位置
        if best is not None and best_key[1] >= CandidateGenerator.MIN_ABS_R:
            return best, True
        return None, False


# ---------------------------------------------------------------- 选择器（可替换）

class BundleSelector(Protocol):
    """选择器协议：Phase 1 规则实现；Phase 2 可换 LLMBundleSelector（同进同出）。"""

    def select(
        self, candidates: list[Candidate], ctx: SelectionContext
    ) -> list[Candidate]: ...


_DIST_TYPES = (ViewType.comparison, ViewType.breakdown, ViewType.ranking)


class RuleBundleSelector:
    """规则选择：骨架 + 边际价值填充 + 去重 + 多样性约束。

    规则（确定性、可解释，每条入选 view 带 selection_reason）：
      A. 核心骨架（主指标驱动）：overview×(1~2) → trend → comparison
         → breakdown → ranking，分布三件套落在不同维度；
         当规则探测到确定性派生比率（如利润率）时，profitability 视角替换
         主指标 ranking 槽位（锚点 breakdown → comparison）；
      B. 去重铁律：同一 (dimension, metric) 不允许仅换 share/group_by/top_n 重复
         （family_key 拦截）；同维度只允许在「跨指标对照」时用不同指标再进一次；
         派生比率（derive_ratio）是同维度允许的第 3 视角，不占原始指标冗余额度；
      C. 余量按边际价值顺序填充到上限 8：
           1) 跨指标对照：第二高价值指标在主指标已用维度上复算（同维度不同指标，
              指标背离/量利错位等跨 View 洞察的基础，优先于 anomaly）；
           2) relationship（独特分析类型，常天然跨指标）；
           3) anomaly（独特分析类型）；
           4) 第二指标趋势；
           5) 第二指标在「新维度」上的分布（类型可重复但维度+指标都必须新）；
           6) 第三个高价值指标总览；
      D. 总数 4-8（数据不支持时不强行凑数，绝不重复造视角）。
    """

    def select(
        self, candidates: list[Candidate], ctx: SelectionContext
    ) -> list[Candidate]:
        chosen: list[Candidate] = []
        used_families: set[tuple] = set()
        used_dist_dims: set[str] = set()   # 已被分布族占用的维度
        used_type_metric: set[tuple] = set()  # (分析类型, 指标)：同类型同指标不重复

        def take(
            c: Candidate, *, contrast: bool = False, reason: str | None = None
        ) -> bool:
            """入选一个候选。contrast=True 表示刻意安排的同维度跨指标对照。"""
            if len(chosen) >= ctx.max_views:
                return False
            if c.family_key in used_families:
                return False
            if c.type in _DIST_TYPES:
                dim = c.dimension_fields[0]
                metric = c.metric_fields[0] if c.metric_fields else None
                if dim in used_dist_dims:
                    # 同维度：仅接受显式安排的跨指标对照（该维度上已存在不同指标）
                    same_dim = [
                        d for d in chosen
                        if d.type in _DIST_TYPES
                        and d.dimension_fields
                        and d.dimension_fields[0] == dim
                    ]
                    other_metric = any(
                        d.metric_fields and d.metric_fields[0] != metric
                        for d in same_dim
                    )
                    if not contrast or not other_metric:
                        return False
                elif (c.type, metric) in used_type_metric:
                    # 新维度但同类型同指标：主指标不在同类型上占第二个维度
                    return False
            if reason is not None:
                c = replace(c, reason=reason)
            chosen.append(c)
            used_families.add(c.family_key)
            if c.type in _DIST_TYPES:
                used_dist_dims.add(c.dimension_fields[0])
                used_type_metric.add(
                    (c.type, c.metric_fields[0] if c.metric_fields else None)
                )
            return True

        by_type: dict[ViewType, list[Candidate]] = {}
        for c in sorted(candidates, key=lambda x: x.priority_group):
            by_type.setdefault(c.type, []).append(c)

        def take_one(
            vt: ViewType, prefer_metric: str | None = None, **kw
        ) -> Candidate | None:
            pool = by_type.get(vt, [])
            if prefer_metric:
                pool = sorted(
                    pool,
                    key=lambda c: 0
                    if c.metric_fields and c.metric_fields[0] == prefer_metric
                    else 1,
                )
            for c in pool:
                if take(c, **kw):
                    return c
            return None

        metrics = ctx.metrics
        m0 = metrics[0].name if metrics else None

        # A. 核心骨架（主指标驱动；第二高价值指标总览立刻形成指标对照）
        take_one(ViewType.overview)
        if len(chosen) < ctx.max_views:
            ov = by_type.get(ViewType.overview, [])
            if len(ov) > 1:
                take(ov[1])
        take_one(ViewType.trend, m0)
        take_one(ViewType.comparison, m0)
        take_one(ViewType.breakdown, m0)

        # profitability 槽位策略（Phase 2 计划批准）：探测到确定性派生比率时，
        # 比率视角替换主指标 ranking 槽位（锚点 = breakdown 维度，其次 comparison）。
        # 候选在对照之后再入选，使顺序为 结构 → 对照 → 利润率。
        prof_pool = by_type.get(ViewType.profitability, [])

        def _anchor_dims() -> list[str]:
            anchors: list[str] = []
            for want in (ViewType.breakdown, ViewType.comparison):
                for d in chosen:
                    if (
                        d.type is want
                        and d.metric_fields
                        and d.metric_fields[0] == m0
                        and d.dimension_fields
                    ):
                        anchors.append(d.dimension_fields[0])
            return list(dict.fromkeys(anchors))

        def _profitability_on_anchors() -> Candidate | None:
            anchors = set(_anchor_dims())
            if not anchors:
                return None
            for c in prof_pool:
                if c.dimension_fields and c.dimension_fields[0] in anchors:
                    return c
            return None

        prof_pick = _profitability_on_anchors()
        # 比率视角将占用 ranking 槽位；无可用锚点时 ranking 照常填充
        if prof_pick is None:
            take_one(ViewType.ranking, m0)

        # C-1. 跨指标对照：第二高价值指标（价值分 ≥3）在主指标已用分布维度上复算。
        #      锚点维度优先 breakdown（结构对照最容易读出量利背离），其次 comparison；
        #      同一锚点上优先 comparison（group_by 口径，避免负值占比）。
        if len(metrics) >= 2 and _metric_score(metrics[1].name) >= 3:
            m1 = metrics[1].name
            anchors: list[str] = []
            for want in (ViewType.breakdown, ViewType.comparison):
                for d in chosen:
                    if (
                        d.type is want
                        and d.metric_fields
                        and d.metric_fields[0] == m0
                        and d.dimension_fields
                    ):
                        anchors.append(d.dimension_fields[0])
            anchors = list(dict.fromkeys(anchors))  # 去重保序

            def _try_contrast() -> bool:
                for dim in anchors:
                    for vt in (ViewType.comparison, ViewType.breakdown):
                        hit = next(
                            (
                                c
                                for c in by_type.get(vt, [])
                                if c.metric_fields
                                and c.metric_fields[0] == m1
                                and c.dimension_fields
                                and c.dimension_fields[0] == dim
                            ),
                            None,
                        )
                        if hit is None:
                            continue
                        if take(
                            hit,
                            contrast=True,
                            reason=(
                                f"第二高价值指标「{m1}」在同一维度「{dim}」上与主指标"
                                f"「{m0}」形成跨指标对照，可识别指标背离/量利错位"
                            ),
                        ):
                            return True
                return False

            _try_contrast()

        # profitability 正式入选：锚点 breakdown → comparison，顺序紧随跨指标对照，
        # 使阅读路径为 主指标结构 → 第二指标对照 → 派生比率（同维度允许第 3 个视角，
        # 但必须是派生比率，不占原始指标冗余额度）。
        if prof_pick is not None:
            for dim in _anchor_dims():
                hit = next(
                    (
                        c
                        for c in prof_pool
                        if c.dimension_fields and c.dimension_fields[0] == dim
                    ),
                    None,
                )
                if hit is None:
                    continue
                num, den = hit.metric_fields[0], hit.metric_fields[1]
                if take(
                    hit,
                    reason=(
                        f"确定性派生比率「{hit.title}」（Σ{num}/Σ{den}，先聚合后相除，"
                        f"LLM 不参与计算）在同一锚点维度「{dim}」上与销售结构、利润对照"
                        f"形成第三视角，替换主指标 ranking 槽位以提升信息价值"
                    ),
                ):
                    break

        # C-2/3. 独特分析类型：关系 → 异常
        take_one(ViewType.relationship)
        take_one(ViewType.anomaly)

        # C-4. 第二指标趋势（价值分 ≥3 才值得一条独立趋势）
        if len(metrics) >= 2 and _metric_score(metrics[1].name) >= 3:
            take_one(ViewType.trend, metrics[1].name)

        # C-5. 第二指标在新维度上的分布（维度 + 指标都必须是新信息）
        if len(metrics) >= 2:
            m1 = metrics[1].name
            for vt in (ViewType.comparison, ViewType.breakdown, ViewType.ranking):
                if len(chosen) >= ctx.max_views:
                    break
                take_one(vt, m1)

        # C-6. 仍有余量：第三个高价值指标总览
        ov = by_type.get(ViewType.overview, [])
        if len(metrics) >= 3 and len(ov) > 2 and _metric_score(metrics[2].name) >= 3:
            take(ov[2])

        return chosen


# ---------------------------------------------------------------- 组装 Bundle

def _to_view(idx: int, c: Candidate) -> AnalysisView:
    plan: AnalysisPlan = build_plan(c.question, c.op, c.hint, c.fields)
    # 图表类型可被候选口径覆盖（如负值组成的份额视角禁用饼图 → share_bar）
    override = c.hint.get("chart_override")
    chart = ChartType(override) if override else VIEW_CHART[c.type]
    return AnalysisView(
        view_id=f"view_{idx:02d}",
        title=c.title,
        type=c.type,
        question=c.question,
        plan=plan,
        chart=chart,
        priority=idx,
        metric_fields=c.metric_fields,
        dimension_fields=c.dimension_fields,
        source="rule.candidate_generation+rule_selection",
        selection_reason=c.reason,
    )


def build_bundle(
    dictionary: DataDictionary,
    df: pd.DataFrame | None,
    *,
    snapshot_rows: int,
    title: str,
    selector: BundleSelector | None = None,
) -> AnalysisBundle:
    """纯函数：字典 + 快照 → AnalysisBundle（不落盘，便于单测与复用）。"""
    selector = selector or RuleBundleSelector()
    metrics = _ranked_metrics(dictionary)
    dims = _ranked_dimensions(dictionary)
    candidates = CandidateGenerator().generate(dictionary, df)
    ctx = SelectionContext(metrics=metrics, dimensions=dims, snapshot_rows=snapshot_rows)
    selected = selector.select(candidates, ctx)
    views = [_to_view(i + 1, c) for i, c in enumerate(selected)]
    return AnalysisBundle(
        bundle_id=f"bundle_{uuid.uuid4().hex[:12]}",
        title=title,
        primary_metrics=[m.name for m in metrics[:2]],
        primary_dimensions=[d.name for d in dims[:3]],
        analysis_views=views,
    )


# ----------------------------------------------------------------- T06 全局筛选

def coerce_filter_values(series: pd.Series, values: list[Any]) -> list[Any]:
    """把 FilterDefinition 的字符串成员转回列实际类型。

    筛选器成员经 astype(str) 生成；数值列直接拿字符串 isin 会零命中，
    因此数值列转回数值，无法转换的保留原字符串。
    """
    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        out: list[Any] = []
        for v in values:
            num = pd.to_numeric(v, errors="coerce")
            out.append(num.item() if pd.notna(num) else v)
        return out
    return [str(v) for v in values]


def normalize_scope(
    snapshot: pd.DataFrame, scope_filters: list[dict[str, Any]]
) -> list[tuple[str, list[Any]]]:
    """固化筛选口径：剔除快照中不存在的列与空 values，并按列实际类型转换。"""
    out: list[tuple[str, list[Any]]] = []
    for f in scope_filters:
        column = str(f.get("column", ""))
        if not column or column not in snapshot.columns:
            continue
        values = [v for v in f.get("values", []) if v is not None and str(v) != ""]
        if not values:
            continue
        out.append((column, coerce_filter_values(snapshot[column], values)))
    return out


def apply_scope_filters(
    df: pd.DataFrame, norm: list[tuple[str, list[Any]]]
) -> pd.DataFrame:
    """在快照/探针数据上裁剪分析范围（与注入 filter 步骤同一口径）。"""
    out = df
    for column, values in norm:
        if column in out.columns:
            out = out[out[column].isin(values)]
    return out


def refine_bundle(
    bundle: AnalysisBundle,
    snapshot: pd.DataFrame,
    scope_filters: list[dict[str, Any]],
) -> AnalysisBundle:
    """全局筛选改变 → 在当前 Bundle 结构上产出新运行版本的 Bundle。

    每个 View 的 Plan 前面插入 in_set filter 步骤（顺序执行即逐层裁剪），
    原步骤 step_id/depends_on 保持不变（execute_plan 按列表顺序执行，
    depends_on 仅为说明）。新 bundle_id；view_id 保持稳定——产物按运行
    目录隔离，不会与旧运行冲突。
    """
    from app.schemas.common import FilterOperator
    from app.schemas.plan import FilterParams, FilterStep

    norm = normalize_scope(snapshot, scope_filters)
    scope_steps = [
        FilterStep(
            step_id=f"s_scope_{i}",
            op="filter",
            description=f"全局筛选：{column} ∈ {values}",
            depends_on=[f"s_scope_{i - 1}"] if i else [],
            params=FilterParams(
                column=column, operator=FilterOperator.in_set, value=values
            ),
        )
        for i, (column, values) in enumerate(norm)
    ]

    new_views: list[AnalysisView] = []
    for view in bundle.analysis_views:
        # 先剥离上一次运行注入的 s_scope_* 步骤（链式 refine/清除筛选时，
        # 否则旧筛选条件会残留在方案里），再插入本次 scope 步骤
        original_steps = [
            s for s in view.plan.steps
            if not str(s.step_id).startswith("s_scope_")
        ]
        new_views.append(view.model_copy(update={
            "plan": view.plan.model_copy(update={
                "steps": [*scope_steps, *original_steps]
            })
        }))
    return AnalysisBundle(
        bundle_id=f"bundle_{uuid.uuid4().hex[:12]}",
        title=bundle.title,
        primary_metrics=bundle.primary_metrics,
        primary_dimensions=bundle.primary_dimensions,
        analysis_views=new_views,
    )
