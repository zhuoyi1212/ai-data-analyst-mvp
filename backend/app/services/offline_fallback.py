"""离线确定性降级（无 LLM key、无回放固件时的规则路径）。

设计原则（与产品红线一致）：
- 只产出受支持的有限算子方案，绝不生成自由代码；
- 洞察中的每个数字都取自 ResultDigest（引擎结果），只做模板化组织，不做任何计算；
- 规则路径是 LLM 的**降级替代**，产物仍经过各自阶段既有的结构/业务/接地校验；
- 语义不明字段仍交给用户确认，不假装确定。

典型触发：用户上传自有 CSV/Excel，而当前未配置 LLM_API_KEY 且无对应回放固件。
"""
from __future__ import annotations

import re
from typing import Any

import pandas as pd

from app.schemas.common import (
    AggFunc,
    Confidence,
    CorrMethod,
    OutlierMethod,
    QuestionCategory,
    SemanticType,
    TimeGranularity,
)
from app.schemas.dictionary import DataDictionary, FieldProfile
from app.schemas.followup import FollowUpQuestion, FollowUpSet
from app.schemas.insight import GroundedNumber, Insight, InsightSet
from app.schemas.plan import (
    AggregateParams,
    AggregateStep,
    AnalysisPlan,
    CorrelationParams,
    CorrelationStep,
    DeriveRatioParams,
    DeriveRatioStep,
    GroupByParams,
    GroupByStep,
    OutlierFlagParams,
    OutlierFlagStep,
    ShareParams,
    ShareStep,
    TimeSeriesParams,
    TimeSeriesStep,
    TopNParams,
    TopNStep,
)
from app.schemas.question import QuestionSet, RecommendedQuestion
from app.services.insight_context import ResultDigest

_DIM_SEMANTICS = {SemanticType.dimension, SemanticType.geo}
_MEAN_HINT = re.compile(
    r"(单价|价格|均价|率$|评分|得分|满意度|时长|平均|均值|单价|price|rate|score|"
    r"satisfaction|duration|avg|unit|mean)",
    re.IGNORECASE,
)
_PRIMARY_HINT = re.compile(
    r"(金额|额$|收入|营收|销售额|销量|花费|费用|总额|amount|revenue|spend|sales|total)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- 字段选择

def _usable(dictionary: DataDictionary) -> list[FieldProfile]:
    return [f for f in dictionary.fields if not f.ignored]


def _metrics(dictionary: DataDictionary) -> list[FieldProfile]:
    return [f for f in _usable(dictionary) if f.semantic_type == SemanticType.metric]


def _dimensions(dictionary: DataDictionary) -> list[FieldProfile]:
    return [f for f in _usable(dictionary) if f.semantic_type in _DIM_SEMANTICS]


def _dates(dictionary: DataDictionary) -> list[str]:
    return dictionary.date_fields()


def _prefer_func(metric_name: str) -> AggFunc:
    return AggFunc.mean if _MEAN_HINT.search(metric_name) else AggFunc.sum


def _primary_metric(dictionary: DataDictionary) -> FieldProfile | None:
    metrics = _metrics(dictionary)
    if not metrics:
        return None
    for m in metrics:
        if _PRIMARY_HINT.search(m.name):
            return m
    return metrics[0]


def _dim_rank(cardinality: int) -> tuple[int, int] | None:
    """维度的分析价值评分：分组对比以 3-12 个成员最佳；单值列无对比意义（排除）。"""
    if cardinality <= 1:
        return None
    if 3 <= cardinality <= 12:
        return (0, cardinality)
    if cardinality == 2:
        return (1, cardinality)  # 二分维度（如 98% vs 2%）信息量低，靠后
    if cardinality <= 30:
        return (2, cardinality)
    return (3, cardinality)  # 高基数（城市/商品名）不适合分组/占比，但适合 Top N


def _ranked_dimensions(dictionary: DataDictionary) -> list[FieldProfile]:
    ranked: list[tuple[tuple[int, int], FieldProfile]] = []
    for f in _dimensions(dictionary):
        rank = _dim_rank(f.cardinality or 0)
        if rank is not None:
            ranked.append((rank, f))
    ranked.sort(key=lambda x: x[0])
    return [f for _, f in ranked]


def _primary_dimension(dictionary: DataDictionary) -> FieldProfile | None:
    dims = _ranked_dimensions(dictionary)
    return dims[0] if dims else None


def _high_cardinality_dimension(
    dictionary: DataDictionary, exclude: set[str]
) -> FieldProfile | None:
    """适合 Top N 排名的高基数维度（城市/商品名等，只取前 N 名即可读）。"""
    cands = [f for f in _dimensions(dictionary)
             if (f.cardinality or 0) > 30 and f.name not in exclude]
    return cands[0] if cands else None


def _granularity(df: pd.DataFrame | None, date_col: str) -> TimeGranularity:
    if df is None or date_col not in df.columns:
        return TimeGranularity.month
    parsed = pd.to_datetime(df[date_col], errors="coerce", format="mixed").dropna()
    if len(parsed) < 2:
        return TimeGranularity.month
    span = (parsed.max() - parsed.min()).days
    if span <= 21:
        return TimeGranularity.day
    if span <= 120:
        return TimeGranularity.week
    return TimeGranularity.month


# ---------------------------------------------------------------- 推荐问题

def fallback_questions(dictionary: DataDictionary, df: pd.DataFrame | None) -> QuestionSet:
    """基于字段类型/关系生成 6-10 条、尽量覆盖 ≥4 分类的推荐问题（含方案参数提示）。"""
    m0 = _primary_metric(dictionary)
    ranked_dims = _ranked_dimensions(dictionary)
    dim0 = ranked_dims[0] if ranked_dims else None
    dim1 = ranked_dims[1] if len(ranked_dims) > 1 else None
    dates = _dates(dictionary)
    metrics = _metrics(dictionary)
    out: list[RecommendedQuestion] = []

    def add(text: str, cat: QuestionCategory, rationale: str,
            fields: list[str], op: str, hint: dict[str, Any]) -> None:
        out.append(RecommendedQuestion(
            text=text, category=cat, rationale=rationale, fields=fields,
            target_op=op, confidence=Confidence.high, plan_hint=hint,
        ))

    if m0 is not None:
        func = _prefer_func(m0.name)
        func_zh = "平均值" if func == AggFunc.mean else "总和"
        add(
            f"「{m0.name}」的{func_zh}是多少？", QuestionCategory.overview,
            f"「{m0.name}」是数值指标，适合先看总体{func_zh}规模",
            [m0.name], "aggregate", {"column": m0.name, "func": func.value},
        )

    if m0 is not None and dates:
        g = _granularity(df, dates[0]).value
        add(
            f"「{m0.name}」随「{dates[0]}」如何变化？", QuestionCategory.trend,
            f"「{dates[0]}」为日期字段，可按{g}观察「{m0.name}」的时间趋势",
            [dates[0], m0.name], "time_series",
            {"date_column": dates[0], "granularity": g,
             "metric": m0.name, "func": _prefer_func(m0.name).value},
        )

    used_dims: set[str] = set()
    if m0 is not None and dim0 is not None:
        f = _prefer_func(m0.name).value
        add(
            f"不同「{dim0.name}」之间「{m0.name}」如何对比？", QuestionCategory.comparison,
            f"「{dim0.name}」是分类维度（{dim0.cardinality} 种取值），可对「{m0.name}」分组对比",
            [dim0.name, m0.name], "group_by",
            {"dimension": dim0.name, "metric": m0.name, "func": f},
        )
        used_dims.add(dim0.name)
        share_dim = dim1 or dim0
        add(
            f"各「{share_dim.name}」的「{m0.name}」占比如何？", QuestionCategory.share,
            f"「{share_dim.name}」是枚举维度，可拆解「{m0.name}」的结构占比",
            [share_dim.name, m0.name], "share",
            {"dimension": share_dim.name, "metric": m0.name, "func": f},
        )
        used_dims.add(share_dim.name)
        # Top N：优先高基数维度（城市/商品排名），否则用与前两问不同的低基数维度
        rank_dim = _high_cardinality_dimension(dictionary, used_dims)
        if rank_dim is None:
            rank_dim = next((d for d in ranked_dims[2:] if d.name not in used_dims), dim0)
        add(
            f"「{m0.name}」最高的「{rank_dim.name}」是哪些？", QuestionCategory.ranking,
            f"可按「{m0.name}」对「{rank_dim.name}」做排名取前五位",
            [rank_dim.name, m0.name], "top_n",
            {"dimension": rank_dim.name, "metric": m0.name, "func": f, "n": 5},
        )

    if m0 is not None:
        add(
            f"「{m0.name}」是否存在异常值？", QuestionCategory.anomaly,
            f"可用 IQR 规则检测「{m0.name}」中的离群异常值",
            [m0.name], "outlier_flag",
            {"column": m0.name, "method": "iqr", "threshold": 1.5},
        )

    # 相关性：优先字典里已探测出的高相关指标对
    corr_pair = _correlation_pair(dictionary, [m.name for m in metrics])
    if corr_pair is not None:
        a, b = corr_pair
        add(
            f"「{a}」与「{b}」的相关性如何？", QuestionCategory.correlation,
            f"「{a}」与「{b}」均为数值指标，可量化其相关关系",
            [a, b], "correlation", {"column_x": a, "column_y": b, "method": "pearson"},
        )

    # 补充到至少 6 条：第二指标概览 / 其他维度对比
    if len(out) < 6:
        for m in metrics:
            if m0 is not None and m.name == m0.name:
                continue
            func = _prefer_func(m.name)
            add(
                f"「{m.name}」的{'平均值' if func == AggFunc.mean else '总和'}是多少？",
                QuestionCategory.overview,
                f"「{m.name}」是数值指标，可补充观察其总体水平",
                [m.name], "aggregate", {"column": m.name, "func": func.value},
            )
            if len(out) >= 6:
                break
    if len(out) < 6 and m0 is not None:
        for d in ranked_dims:
            if d.name in used_dims:
                continue
            add(
                f"不同「{d.name}」的「{m0.name}」如何分布？", QuestionCategory.comparison,
                f"「{d.name}」是分类维度，可换维度对「{m0.name}」分组",
                [d.name, m0.name], "group_by",
                {"dimension": d.name, "metric": m0.name,
                 "func": _prefer_func(m0.name).value},
            )
            if len(out) >= 6:
                break

    return QuestionSet(questions=out[:10])


def _correlation_pair(
    dictionary: DataDictionary, metric_names: list[str]
) -> tuple[str, str] | None:
    for rel in dictionary.relations:
        if rel.type == "correlation" and len(rel.columns) >= 2:
            return rel.columns[0], rel.columns[1]
    if len(metric_names) >= 2:
        return metric_names[0], metric_names[1]
    return None


# ---------------------------------------------------------------- 方案构造

def _step(op: str, hint: dict[str, Any], fields: list[str]):
    if op == "aggregate":
        column = hint.get("column") or (fields[0] if fields else None)
        func = AggFunc(hint.get("func", "sum"))
        return AggregateStep(
            step_id="agg", op="aggregate",
            params=AggregateParams(column=column, func=func),
            description=f"对字段「{column or '记录'}」执行{func.value}",
        )
    if op == "group_by":
        p = GroupByParams(
            dimension=hint["dimension"],
            metric=hint.get("metric"), func=AggFunc(hint.get("func", "sum")),
        )
        return GroupByStep(step_id="group_by", op="group_by", params=p,
                           description=f"按「{p.dimension}」分组聚合「{p.metric or '记录数'}」")
    if op == "share":
        p = ShareParams(
            dimension=hint["dimension"],
            metric=hint.get("metric"), func=AggFunc(hint.get("func", "sum")),
        )
        return ShareStep(step_id="share", op="share", params=p,
                         description=f"按「{p.dimension}」拆解「{p.metric or '记录数'}」占比")
    if op == "top_n":
        p = TopNParams(
            dimension=hint["dimension"], metric=hint.get("metric"),
            func=AggFunc(hint.get("func", "sum")), n=int(hint.get("n", 5)),
        )
        return TopNStep(step_id="top_n", op="top_n", params=p,
                        description=f"按「{p.metric or '记录数'}」取「{p.dimension}」前 {p.n} 名")
    if op == "time_series":
        p = TimeSeriesParams(
            date_column=hint.get("date_column") or fields[0],
            granularity=TimeGranularity(hint.get("granularity", "month")),
            metric=hint.get("metric"), func=AggFunc(hint.get("func", "sum")),
        )
        return TimeSeriesStep(step_id="time_series", op="time_series", params=p,
                              description=f"按{p.granularity.value}聚合「{p.metric or '记录数'}」")
    if op == "correlation":
        p = CorrelationParams(
            column_x=hint.get("column_x") or fields[0],
            column_y=hint.get("column_y") or fields[1],
            method=CorrMethod(hint.get("method", "pearson")),
        )
        return CorrelationStep(step_id="correlation", op="correlation", params=p,
                               description=f"计算「{p.column_x}」与「{p.column_y}」相关系数")
    if op == "outlier_flag":
        p = OutlierFlagParams(
            column=hint.get("column") or fields[0],
            method=OutlierMethod(hint.get("method", "iqr")),
            threshold=float(hint.get("threshold", 1.5)),
        )
        return OutlierFlagStep(step_id="outlier_flag", op="outlier_flag", params=p,
                               description=f"用 {p.method.value} 规则标记「{p.column}」离群值")
    if op == "derive_ratio":
        p = DeriveRatioParams(
            numerator=hint["numerator"],
            denominator=hint["denominator"],
            dimension=hint.get("dimension"),
            date_column=hint.get("date_column"),
            granularity=TimeGranularity(hint["granularity"]) if hint.get("granularity") else None,
        )
        if p.dimension:
            axis = f"（按「{p.dimension}」）"
        elif p.date_column:
            axis = f"（按{p.granularity.value if p.granularity else ''}趋势）"
        else:
            axis = "（整体）"
        return DeriveRatioStep(
            step_id="derive_ratio", op="derive_ratio", params=p,
            description=f"确定性派生比率「{p.numerator}」÷「{p.denominator}」{axis}",
        )
    raise ValueError(f"离线降级不支持算子: {op}")


_SHAPE_ZH = {
    "aggregate": "单行单列指标卡", "group_by": "按维度分组的多行结果",
    "share": "各成员值与占比", "top_n": "排名前 N 的成员",
    "time_series": "按时间周期排列的序列", "correlation": "相关系数单值",
    "outlier_flag": "带离群标记的结果", "derive_ratio": "派生比率（单值/按维度/按时间）",
}


def build_plan(
    question: str, op: str, hint: dict[str, Any], fields: list[str]
) -> AnalysisPlan:
    step = _step(op, hint, fields)
    return AnalysisPlan(
        question=question,
        data_scope="数据质量处理后的全部数据，无额外筛选条件",
        steps=[step],
        expected_shape=_SHAPE_ZH.get(op, "分析结果"),
        chart_hint=None,
    )


def _match_stored_question(
    question: str, store, session_id: str
) -> tuple[str, dict[str, Any], list[str]] | None:
    """在已落盘的推荐问题/追问中按文本精确匹配，取回算子与参数提示。"""
    candidates: list[tuple[str, dict[str, Any], list[str]]] = []
    for artifact_name in ("questions", "followups"):
        if not store.has_artifact(session_id, artifact_name):
            continue
        data = store.read_artifact(session_id, artifact_name)
        for q in data.get("questions", []):
            if q.get("text") == question:
                candidates.append(
                    (q["target_op"], q.get("plan_hint", {}), q.get("fields", []))
                )
    return candidates[0] if candidates else None


_KEYWORD_RULES = [
    ("correlation", re.compile(r"相关|correlation")),
    ("time_series", re.compile(r"趋势|随时间|按月|按周|按日|变化|走势|time")),
    ("share", re.compile(r"占比|比例|构成|份额|percent|share")),
    ("outlier_flag", re.compile(r"异常|离群|极端|outlier")),
    ("top_n", re.compile(r"排名|前\s*\d|前几|最高的.{0,8}(哪些|是谁)|top")),
    ("group_by", re.compile(r"对比|各(个|种|类|地区|渠道|区域|品类)?|不同|分组|分布|by")),
]


def _infer_op(question: str) -> str:
    for op, pat in _KEYWORD_RULES:
        if pat.search(question):
            return op
    if re.search(r"平均|均值|mean", question):
        return "aggregate"
    return "aggregate"


def plan_for_question(
    question: str, dictionary: DataDictionary, store, session_id: str,
    df: pd.DataFrame | None,
) -> AnalysisPlan:
    """为任意问题确定性构造方案：优先匹配已推荐问题，其次关键词+字段名推断。"""
    matched = _match_stored_question(question, store, session_id)
    if matched is not None:
        op, hint, fields = matched
        return build_plan(question, op, hint, fields)

    # 自定义问题：推断算子 + 从问题文本提取出现过的字段名
    names = {f.name for f in _usable(dictionary)}
    in_text = [n for n in names if n in question]
    op = _infer_op(question)
    m0 = _primary_metric(dictionary)
    dim0 = _primary_dimension(dictionary)
    dates = _dates(dictionary)
    metrics = [m.name for m in _metrics(dictionary)]
    metric_in_text = [n for n in in_text if n in metrics]
    dim_in_text = [n for n in in_text
                   if dictionary.field(n) and dictionary.field(n).semantic_type in _DIM_SEMANTICS]
    date_in_text = [n for n in in_text if n in dates]

    hint: dict[str, Any] = {}
    fields: list[str] = in_text
    if op == "aggregate":
        col = (metric_in_text[0] if metric_in_text else (m0.name if m0 else None))
        func = AggFunc.mean if re.search(r"平均|均值", question) else _prefer_func(col or "")
        hint = {"column": col, "func": func.value}
        fields = [col] if col else []
    elif op == "time_series":
        dc = date_in_text[0] if date_in_text else (dates[0] if dates else None)
        metric = metric_in_text[0] if metric_in_text else (m0.name if m0 else None)
        if dc is None or metric is None:
            return build_plan(question, "aggregate",
                              {"column": m0.name if m0 else None,
                               "func": _prefer_func(m0.name or "").value},
                              [m0.name] if m0 else [])
        hint = {"date_column": dc, "granularity": _granularity(df, dc).value,
                "metric": metric, "func": _prefer_func(metric).value}
        fields = [dc, metric]
    elif op in {"group_by", "share", "top_n"}:
        dim = dim_in_text[0] if dim_in_text else (dim0.name if dim0 else None)
        metric = metric_in_text[0] if metric_in_text else (m0.name if m0 else None)
        if dim is None:
            return build_plan(question, "aggregate",
                              {"column": metric, "func": _prefer_func(metric or "").value},
                              [metric] if metric else [])
        hint = {"dimension": dim, "metric": metric, "func": _prefer_func(metric or "").value}
        if op == "top_n":
            hint["n"] = 5
        fields = [dim, metric] if metric else [dim]
    elif op == "correlation":
        if len(metric_in_text) >= 2:
            a, b = metric_in_text[0], metric_in_text[1]
        elif len(metrics) >= 2:
            a, b = metrics[0], metrics[1]
        else:
            return build_plan(question, "aggregate",
                              {"column": m0.name if m0 else None,
                               "func": _prefer_func(m0.name or "").value},
                              [m0.name] if m0 else [])
        hint = {"column_x": a, "column_y": b, "method": "pearson"}
        fields = [a, b]
    elif op == "outlier_flag":
        col = metric_in_text[0] if metric_in_text else (m0.name if m0 else None)
        hint = {"column": col, "method": "iqr", "threshold": 1.5}
        fields = [col] if col else []
    return build_plan(question, op, hint, fields)


# ---------------------------------------------------------------- 追问

def fallback_followups(
    dictionary: DataDictionary, digest: ResultDigest, df: pd.DataFrame | None
) -> FollowUpSet:
    """基于字段与当前结果生成 3 条可继续下钻的追问（尽量换维度/换算子）。"""
    m0 = _primary_metric(dictionary)
    ranked_dims = _ranked_dimensions(dictionary)
    dates = _dates(dictionary)
    metrics = [m.name for m in _metrics(dictionary)]
    current_op = digest.terminal_op
    questions: list[FollowUpQuestion] = []

    def add(text, rationale, fields, op, hint) -> None:
        if op == current_op:
            return
        questions.append(FollowUpQuestion(
            text=text, rationale=rationale, fields=fields,
            target_op=op, plan_hint=hint,
        ))

    f0 = _prefer_func(m0.name).value if m0 else "sum"

    # 三个维度类追问使用不同维度：最佳分组维度 / 次佳维度 / 高基数排名维度
    d_group = ranked_dims[0] if ranked_dims else None
    d_share = ranked_dims[1] if len(ranked_dims) > 1 else d_group
    used = {d.name for d in [d_group, d_share] if d is not None}
    d_rank = _high_cardinality_dimension(dictionary, used)
    if d_rank is None:
        d_rank = next((d for d in ranked_dims[2:] if d.name not in used), d_group)

    if m0 is not None and d_group is not None:
        add(f"不同「{d_group.name}」之间「{m0.name}」如何对比？",
            f"「{d_group.name}」是分类维度（{d_group.cardinality} 种取值），可对「{m0.name}」继续分组下钻",
            [d_group.name, m0.name], "group_by",
            {"dimension": d_group.name, "metric": m0.name, "func": f0})
    if m0 is not None and d_share is not None:
        add(f"各「{d_share.name}」的「{m0.name}」占比如何？",
            f"可换用占比视角拆解「{d_share.name}」维度上的「{m0.name}」结构",
            [d_share.name, m0.name], "share",
            {"dimension": d_share.name, "metric": m0.name, "func": f0})
    if m0 is not None and d_rank is not None:
        add(f"「{m0.name}」最高的「{d_rank.name}」是哪些？",
            f"可对「{d_rank.name}」按「{m0.name}」做排名取前五",
            [d_rank.name, m0.name], "top_n",
            {"dimension": d_rank.name, "metric": m0.name, "func": f0, "n": 5})

    if m0 is not None and dates:
        g = _granularity(df, dates[0]).value
        add(f"「{m0.name}」随「{dates[0]}」的趋势如何？",
            f"「{dates[0]}」为日期字段，可按{g}观察「{m0.name}」趋势",
            [dates[0], m0.name], "time_series",
            {"date_column": dates[0], "granularity": g,
             "metric": m0.name, "func": f0})

    if m0 is not None:
        add(f"「{m0.name}」是否存在异常值？",
            f"可用 IQR 规则继续检测「{m0.name}」的离群值",
            [m0.name], "outlier_flag",
            {"column": m0.name, "method": "iqr", "threshold": 1.5})

    pair = _correlation_pair(dictionary, metrics)
    if pair is not None:
        a, b = pair
        add(f"「{a}」与「{b}」的相关性如何？",
            f"「{a}」与「{b}」均为数值指标，可量化相关关系",
            [a, b], "correlation", {"column_x": a, "column_y": b, "method": "pearson"})

    chosen = questions[:3]
    return FollowUpSet(questions=chosen)


# ---------------------------------------------------------------- 洞察（数字全部取自 digest）

def _gn(p, label: str) -> GroundedNumber:
    return GroundedNumber(value=p.value, label=label, ref_step=p.step_id,
                          ref_kind=p.kind, ref_keys=dict(p.keys))


def fallback_insights(digest: ResultDigest) -> InsightSet:
    op = digest.terminal_op
    points = digest.points
    insights: list[Insight] = []

    if op == "aggregate":
        p = points[0]
        insights = [
            Insight(text=f"该指标的计算结果为 {p.value}，基于质量处理后的全部数据得出。",
                    type="key_finding", evidence_refs=[p.step_id], numbers=[_gn(p, "聚合结果")],
                    confidence=Confidence.high),
            Insight(text=f"计算引擎返回的结果值为 {p.value}，口径与公式可在证据链中查看。",
                    type="key_finding", evidence_refs=[p.step_id], numbers=[_gn(p, "聚合结果")],
                    confidence=Confidence.high),
        ]
    elif op in {"group_by", "top_n", "compare_groups"}:
        rows = digest.rows
        if rows:
            dim = next(k for k in rows[0].keys() if k != "value")
            top, bot = rows[0], rows[-1]
            p_top = next(p for p in points if p.keys == {dim: str(top[dim])})
            p_bot = next(p for p in points if p.keys == {dim: str(bot[dim])})
            insights = [
                Insight(text=f"在「{dim}」维度下，{top[dim]} 的数值最高，为 {top['value']}。",
                        type="comparison", evidence_refs=[p_top.step_id],
                        numbers=[_gn(p_top, str(top[dim]))], confidence=Confidence.high),
                Insight(text=f"排序最末的成员是 {bot[dim]}，对应数值为 {bot['value']}。",
                        type="comparison", evidence_refs=[p_bot.step_id],
                        numbers=[_gn(p_bot, str(bot[dim]))], confidence=Confidence.high),
            ]
    elif op == "share":
        rows = digest.rows
        if rows:
            dim = next(k for k in rows[0].keys() if k not in {"value", "share_pct"})
            top = rows[0]
            p_val = next(p for p in points if p.keys == {dim: str(top[dim])} and not p.is_percent)
            p_pct = next(p for p in points
                         if p.keys == {dim: str(top[dim]), "metric": "share_pct"})
            insights = [
                Insight(text=f"占比最高的成员是 {top[dim]}，占比 {top['share_pct']}%，数值为 {top['value']}。",
                        type="comparison", evidence_refs=[p_val.step_id],
                        numbers=[_gn(p_val, f"{top[dim]} 数值"), _gn(p_pct, f"{top[dim]} 占比")],
                        confidence=Confidence.high),
            ]
            if len(rows) > 1:
                second = rows[1]
                p2 = next(p for p in points
                          if p.keys == {dim: str(second[dim]), "metric": "share_pct"})
                insights.append(Insight(
                    text=f"其次为 {second[dim]}，占比 {second['share_pct']}%。",
                    type="comparison", evidence_refs=[p2.step_id],
                    numbers=[_gn(p2, f"{second[dim]} 占比")], confidence=Confidence.high))
    elif op == "time_series":
        rows = [r for r in digest.rows if r["value"] is not None]
        if rows:
            date_col = next(k for k in rows[0].keys() if k != "value")
            top = max(rows, key=lambda r: r["value"])
            bot = min(rows, key=lambda r: r["value"])
            p_top = next(p for p in points if p.keys == {date_col: str(top[date_col])})
            p_bot = next(p for p in points if p.keys == {date_col: str(bot[date_col])})
            insights = [
                Insight(text=f"{top[date_col]} 出现周期峰值，数值为 {top['value']}。",
                        type="trend", evidence_refs=[p_top.step_id],
                        numbers=[_gn(p_top, str(top[date_col]))], confidence=Confidence.high),
                Insight(text=f"{bot[date_col]} 为周期谷值，数值为 {bot['value']}。",
                        type="trend", evidence_refs=[p_bot.step_id],
                        numbers=[_gn(p_bot, str(bot[date_col]))], confidence=Confidence.high),
            ]
    elif op == "correlation":
        p_r = next(p for p in points if p.keys == {"field": "coefficient"})
        p_n = next(p for p in points if p.keys == {"field": "n"})
        r = p_r.value
        direction = "正相关" if r > 0 else ("负相关" if r < 0 else "无线性相关")
        strength = "强相关" if abs(r) >= 0.6 else ("中等相关" if abs(r) >= 0.3 else "弱相关")
        insights = [
            Insight(text=f"两字段相关系数为 {r}（样本对数 {p_n.value:.0f}），呈{strength}{direction}（相关关系，不代表因果）。",
                    type="risk", evidence_refs=[p_r.step_id],
                    numbers=[_gn(p_r, "相关系数"), _gn(p_n, "样本对数")],
                    confidence=Confidence.medium, needs_further_validation=True,
                    disclaimer="相关关系，不代表因果"),
            Insight(text=f"基于 {p_n.value:.0f} 对样本计算，相关系数为 {r}，呈{direction}（相关关系，不代表因果）。",
                    type="risk", evidence_refs=[p_r.step_id],
                    numbers=[_gn(p_r, "相关系数"), _gn(p_n, "样本对数")],
                    confidence=Confidence.medium, needs_further_validation=True,
                    disclaimer="相关关系，不代表因果"),
        ]
    elif op == "outlier_flag":
        p_count = next(p for p in points if p.keys == {"field": "outlier_count"})
        p_rate = next(p for p in points if p.keys == {"field": "outlier_rate_pct"})
        p_lo = next(p for p in points if p.keys == {"field": "lower_bound"})
        p_hi = next(p for p in points if p.keys == {"field": "upper_bound"})
        insights = [
            Insight(text=f"在 IQR 判定区间 [{p_lo.value}, {p_hi.value}] 之外，共识别 {p_count.value:.0f} 个离群点。",
                    type="anomaly", evidence_refs=[p_count.step_id],
                    numbers=[_gn(p_lo, "下界"), _gn(p_hi, "上界"), _gn(p_count, "离群点数")],
                    confidence=Confidence.high),
            Insight(text=f"离群点占比为 {p_rate.value}%，建议结合业务判断其是否为真实异常。",
                    type="anomaly", evidence_refs=[p_rate.step_id],
                    numbers=[_gn(p_rate, "离群占比")], confidence=Confidence.high),
        ]
    elif op == "period_compare":
        p_cur = next(p for p in points if p.keys == {"field": "current_value"})
        p_prev = next(p for p in points if p.keys == {"field": "previous_value"})
        p_g = next(p for p in points if p.keys == {"field": "growth_pct"})
        word = "上升" if p_g.value > 0 else "下降"
        insights = [
            Insight(text=f"本期值为 {p_cur.value}，上期值为 {p_prev.value}，环比{word} {abs(p_g.value)}%。",
                    type="trend", evidence_refs=[p_cur.step_id],
                    numbers=[_gn(p_cur, "本期值"), _gn(p_prev, "上期值"), _gn(p_g, "变化率")],
                    confidence=Confidence.high),
            Insight(text=f"相较上期 {p_prev.value}，本期为 {p_cur.value}，变化幅度 {abs(p_g.value)}%。",
                    type="trend", evidence_refs=[p_cur.step_id],
                    numbers=[_gn(p_cur, "本期值"), _gn(p_prev, "上期值"), _gn(p_g, "变化率")],
                    confidence=Confidence.high),
        ]

    if not insights:
        # 终极兜底：不写任何无法接地的数字，仅引用结果摘要（理论上不会走到）
        raise ValueError(f"离线洞察暂不支持算子: {op}")
    return InsightSet(insights=insights)
