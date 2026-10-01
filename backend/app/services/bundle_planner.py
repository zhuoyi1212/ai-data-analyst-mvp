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
import warnings
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
    role: str = "presentation"  # T07：presentation（Seed 展示）/ computation（内部证据）


@dataclass
class SelectionContext:
    """选择阶段上下文（规则与未来 LLM 选择器共用）。"""

    metrics: list[FieldProfile]
    dimensions: list[FieldProfile]
    snapshot_rows: int
    max_views: int = 12


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

        # 2) trend：两个核心主指标各有趋势并对齐（即使是 DAU/新增、
        #    入库/出库、时长/满意度等非金额指标）；排名 top-2 天然把
        #    「单价」等第三位率指标排除在对齐之外
        if date_col and gran is not None and metrics:
            primary = metrics[0]
            extra = metrics[1:2]
            for i, m in enumerate([primary] + extra):
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
                    # family_key 与 value_filter._family_signature 保持同构：
                    # 第三槽位为 (分子, 分母)，两侧都能仅凭视图数据算出
                    family_key=(
                        "ratio", d.name,
                        (spec.numerator, spec.denominator),
                    ),
                    reason=(
                        f"规则探测到确定性派生指标「{spec.label}」"
                        f"=Σ{spec.numerator}/Σ{spec.denominator}（先聚合后相除），"
                        f"按「{d.name}」诊断盈利结构，能识别收入大但亏损的成员"
                    ),
                    priority_group=3,
                ))

        # 3.6) T08 跨指标信号（锚定全部低基数维度，Selector 只保留基准
        #      拆分维度；全部 role=computation，默认隐藏，Finding 引用时透出）。
        if date_col and gran is not None and low_dims:
            # (a) 分组变化贡献：可加 sum 指标；count_distinct 仅在业务键
            #     已确认（entity_key）时启用（Orders/AOV 规则）。
            for m in metrics:
                ms = specs.get(m.name)
                agg = _spec_func(ms)
                eligible = (
                    ms is not None and ms.additive
                    and ms.aggregation == "sum"
                ) or (
                    ms is not None
                    and ms.aggregation == "count_distinct"
                    and bool(ms.entity_key)
                )
                if not eligible:
                    continue
                for d in low_dims:
                    out.append(Candidate(
                        type=ViewType.contribution,
                        title=f"「{m.name}」变化的「{d.name}」贡献分解",
                        question=(
                            f"「{m.name}」最近一期相对上一期的变化，"
                            f"各「{d.name}」分别贡献多少（会计拆解）？"
                        ),
                        op="contribution",
                        hint={
                            "date_column": date_col, "metric": m.name,
                            "dimension": d.name, "func": agg.value,
                            "period": "mom",
                        },
                        fields=[date_col, m.name, d.name],
                        metric_fields=[m.name],
                        dimension_fields=[d.name],
                        family_key=("contrib", d.name, m.name),
                        reason=(
                            f"「{m.name}」口径 {'可加总和' if ms.additive else '业务键已确认的去重计数'}，"
                            f"变化可按「{d.name}」做加法恒等式分解并回算"
                        ),
                        priority_group=3,
                        role="computation",
                    ))

            # (b) 率的结构变化：必须有分子、分母两个原始字段（事件分母）。
            for rspec in detect_derived_metrics(dictionary):
                for d in low_dims:
                    out.append(Candidate(
                        type=ViewType.rate_shift,
                        title=f"「{rspec.label}」变化的「{d.name}」结构分解",
                        question=(
                            f"「{rspec.label}」的变化中，各组率变化（within）"
                            f"与结构权重变化（mix）各占多少？"
                        ),
                        op="rate_decomposition",
                        hint={
                            "date_column": date_col,
                            "numerator": rspec.numerator,
                            "denominator": rspec.denominator,
                            "dimension": d.name, "period": "mom",
                        },
                        fields=[date_col, rspec.numerator,
                                rspec.denominator, d.name],
                        metric_fields=[rspec.numerator, rspec.denominator],
                        dimension_fields=[d.name],
                        family_key=(
                            "rateshift", d.name,
                            (rspec.numerator, rspec.denominator),
                        ),
                        reason=(
                            f"规则探测到分子「{rspec.numerator}」与事件分母"
                            f"「{rspec.denominator}」，可分解 within/mix 效应，"
                            f"识别「各组改善、整体下降」的结构悖论"
                        ),
                        priority_group=3,
                        role="computation",
                    ))

        # 4) relationship：优先字典探测到的相关对；否则在快照上确定性计算
        #    （含率类指标对优先）。T07：无论强弱都作为内部证据任务生成——
        #    弱 r（含 r=0）默认不占展示位，但用户明确问到时可返回阴性结果；
        #    样本不足（无法计算）不生成。
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
                    "快照规则计算中该指标对相关性最显著（含率/折扣类指标优先）；"
                    "作为内部证据任务，|r| 显著才进入默认展示"
                    if pair_computed
                    else "字典规则已探测到该指标对的相关性（或它们是两个主要数值指标）；"
                         "作为内部证据任务，|r| 显著才进入默认展示"
                ),
                priority_group=5,
                role="computation",
            ))

        # 5) anomaly：主指标离群（样本足够才生成）。T07：内部证据任务——
        #    有离群才形成 Finding/默认展示，无离群默认隐藏（用户问起可返回阴性）。
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
                reason=f"{rows} 行样本下用 IQR 1.5 倍规则识别「{m.name}」离群；"
                       f"作为内部证据任务，发现离群才进入默认展示",
                priority_group=4,
                role="computation",
            ))

        return out

    @staticmethod
    def _best_correlation_pair(
        dictionary: DataDictionary,
        metrics: list[FieldProfile],
        df: pd.DataFrame | None,
    ) -> tuple[tuple[str, str] | None, bool]:
        """返回 ((x, y), computed)。

        T07：不再因 |r| 偏弱返回 None——弱 r（含 0）的关系视角作为内部
        证据任务保留，由价值筛选默认隐藏；仅在无法取得任何指标对时返回 None。
        """
        rels = [
            r for r in dictionary.relations
            if r.type == "correlation" and len(r.columns) >= 2
            and r.coefficient is not None
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
        # best 即最佳指标对（r 可能为 0/很弱）：作为证据任务返回，
        # 是否进入默认展示交给价值筛选（T07）
        if best is not None:
            return best, True
        return None, False


# ---------------------------------------------------------------- 选择器（可替换）

class BundleSelector(Protocol):
    """选择器协议：Phase 1 规则实现；Phase 2 可换 LLMBundleSelector（同进同出）。"""

    def select(
        self, candidates: list[Candidate], ctx: SelectionContext
    ) -> list[Candidate]: ...


class RuleBundleSelector:
    """T07 Seed 选择：聚焦核心，不按图表类型凑数量。

    Seed（role=presentation，固定顺序，保证执行且默认可见）：
      1. 核心 KPI：overview m0；m1 价值分≥3 时追加 overview m1（至多 2 个）；
      2. 对齐趋势：排名 top-2 的两个核心主指标各一趋势（无条件对齐，
         含 DAU/新增、入库/出库等非金额组合），双指标趋势不被任何
         槽位挤掉（T07 验收）；
      3. 基准拆分：m0 在最佳低基数维度上的 breakdown（一个，不做同维重复图）；
    证据任务（role=computation，默认隐藏，结果保留可按需返回），按边际价值顺序：
      relationship（最佳指标对，含弱 r/r=0）
        → anomaly（m0 离群）
        → contribution（m0/m1 基准维度变化贡献，T08）
        → m1 在基准维度上的 comparison（跨指标对照 / 量利背离证据）
        → profitability（基准维度派生比率）
        → rate_shift（基准维度率结构变化，T08）
        → 额外维度 comparison / ranking（m0、m1），
      全部经 family_key 去重，总数受 ctx.max_views 封顶；数据不支持时绝不凑数。
    """

    def select(
        self, candidates: list[Candidate], ctx: SelectionContext
    ) -> list[Candidate]:
        by_type: dict[ViewType, list[Candidate]] = {}
        for c in candidates:
            by_type.setdefault(c.type, []).append(c)

        metrics = ctx.metrics
        m0 = metrics[0].name if metrics else None
        # m1（≥3）用于核心 KPI 追加；趋势对齐则无条件覆盖 top-2 主指标
        m1_trend = metrics[1].name if len(metrics) > 1 else None
        m1 = (
            m1_trend
            if m1_trend is not None and _metric_score(m1_trend) >= 3
            else None
        )

        def _find(
            vt: ViewType, metric: str | None = None, dim: str | None = None
        ) -> Candidate | None:
            for c in by_type.get(vt, []):
                if metric is not None and not (
                    c.metric_fields and c.metric_fields[0] == metric
                ):
                    continue
                if dim is not None and not (
                    c.dimension_fields and c.dimension_fields[0] == dim
                ):
                    continue
                return c
            return None

        # ------------------------------------------------ Seed（presentation）
        seed: list[Candidate] = []
        ov0 = _find(ViewType.overview, m0) if m0 else None
        ov1 = _find(ViewType.overview, m1) if m1 else None
        for c in (ov0, ov1):
            if c is not None:
                seed.append(c)

        t0 = _find(ViewType.trend, m0) if m0 else None
        t1 = _find(ViewType.trend, m1_trend) if m1_trend else None
        for metric, c in ((m0, t0), (m1_trend, t1)):
            if c is not None:
                seed.append(c)
            elif metric is not None:
                # 不再静默跳过：核心指标的对齐趋势缺失时显式记录，
                # 便于定位「无日期字段 / 聚合口径不支持」等真实原因
                warnings.warn(
                    f"核心指标「{metric}」缺少趋势候选，Seed 未包含其对齐趋势"
                    f"（可能无日期字段或该指标无支持的聚合口径）",
                    RuntimeWarning,
                    stacklevel=2,
                )

        # 基准拆分：同指标所有 breakdown 候选中，按维度信息量挑选——
        # 2-3 成员过粗，6-12 成员结构最可读，>20 过杂；同分时保留候选生成顺序。
        breakdowns = [
            c for c in by_type.get(ViewType.breakdown, [])
            if m0 and c.metric_fields and c.metric_fields[0] == m0
        ]

        def _dim_info(dim: str | None) -> int:
            if dim is None:
                return -1
            card = next(
                (d.cardinality or 0 for d in ctx.dimensions if d.name == dim), 0
            )
            # 4-8 成员结构最可读；3 个过粗、9-15 略杂；2 或 16-20 只作兜底
            if 4 <= card <= 8:
                return 3
            if card == 3 or 9 <= card <= 15:
                return 2
            if card == 2 or 16 <= card <= 20:
                return 1
            return 0

        b0 = max(breakdowns, key=lambda c: _dim_info(c.dimension_fields[0]), default=None)
        if b0 is not None:
            seed.append(b0)
        baseline_dim = b0.dimension_fields[0] if b0 else None

        # -------------------------------------------- 证据任务（computation）
        ordered: list[Candidate] = []
        rel = _find(ViewType.relationship)
        if rel is not None:
            ordered.append(rel)
        anom = _find(ViewType.anomaly, m0) if m0 else None
        if anom is not None:
            ordered.append(anom)
        # T08：m0 变化贡献（基准维度），规模-盈利背离的量化证据
        if baseline_dim:
            contrib0 = _find(ViewType.contribution, m0, baseline_dim)
            if contrib0 is not None:
                ordered.append(contrib0)
        if m1 and baseline_dim:
            contrast = _find(ViewType.comparison, m1, baseline_dim)
            if contrast is not None:
                ordered.append(contrast)
            # T08：m1 变化贡献（如 Profit 下滑由谁构成）
            contrib1 = _find(ViewType.contribution, m1, baseline_dim)
            if contrib1 is not None:
                ordered.append(contrib1)
        if baseline_dim:
            prof = _find(ViewType.profitability, dim=baseline_dim)
            if prof is not None:
                ordered.append(prof)
            # T08：率结构变化（Simpson 悖论证据）
            rateshift = _find(ViewType.rate_shift, dim=baseline_dim)
            if rateshift is not None:
                ordered.append(rateshift)
        # 余量证据：额外维度 comparison / ranking（m0、m1）；不挑无证据的图
        for vt in (ViewType.comparison, ViewType.ranking):
            for c in by_type.get(vt, []):
                if c.metric_fields and c.metric_fields[0] in (m0, m1):
                    ordered.append(c)

        used_families = {c.family_key for c in seed}
        seen: set[int] = {id(c) for c in seed}
        comps: list[Candidate] = []
        for c in ordered:
            if id(c) in seen:
                continue
            seen.add(id(c))
            if c.family_key in used_families:
                continue
            used_families.add(c.family_key)
            if c.role != "computation":
                c = replace(c, role="computation")
            comps.append(c)
            if len(seed) + len(comps) >= ctx.max_views:
                break

        return [*seed, *comps]


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
        role=c.role,
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
