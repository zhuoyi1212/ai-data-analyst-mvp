"""有限算子注册表（附录 A 的唯一事实来源）。

包含：算子元数据、人读模板、末端算子→图表的确定性映射、
以及方案对数据字典的语义合法性校验。执行器（ops.py）只允许实现这里登记的算子。
"""
from __future__ import annotations

from app.schemas.common import ChartType, FieldInfo, FilterOperator, SemanticType
from app.schemas.plan import (
    AnalysisPlan,
    FilterStep,
    PlanStep,
)

# op_id -> 元数据
OP_META: dict[str, dict] = {
    "filter": {"label": "筛选", "terminal": False, "chart": None},
    "aggregate": {"label": "总量聚合", "terminal": True, "chart": ChartType.metric},
    "group_by": {"label": "分组聚合", "terminal": True, "chart": ChartType.bar},
    "share": {"label": "占比", "terminal": True, "chart": ChartType.share_bar},
    "top_n": {"label": "Top N 排名", "terminal": True, "chart": ChartType.bar},
    "time_series": {"label": "时间趋势", "terminal": True, "chart": ChartType.line},
    "period_compare": {
        "label": "同环比",
        "terminal": True,
        "chart": ChartType.metric_compare,
    },
    "compare_groups": {
        "label": "分组对比",
        "terminal": True,
        "chart": ChartType.grouped_bar,
    },
    "correlation": {"label": "相关分析", "terminal": True, "chart": ChartType.scatter},
    "outlier_flag": {
        "label": "离群检测",
        "terminal": True,
        "chart": ChartType.line_outlier,
    },
}

# 末端算子（除 filter 透传外的所有算子）→ 图表类型
CHART_BY_OP: dict[str, ChartType] = {
    op: meta["chart"] for op, meta in OP_META.items() if meta["chart"] is not None
}

TIME_OPS = {"time_series", "period_compare"}
DIMENSION_SEMANTICS = {SemanticType.dimension, SemanticType.geo}

# 不要求指标列为 metric 语义的聚合函数
COUNT_FUNCS = {"count", "count_distinct"}
_VALUELESS_FILTER_OPS: set[FilterOperator] = set()
_LIST_FILTER_OPS = {FilterOperator.in_set, FilterOperator.not_in, FilterOperator.between}


def known_ops() -> set[str]:
    return set(OP_META)


def step_columns(step: PlanStep) -> list[str]:
    """返回一个步骤引用的全部列名。"""
    p = step.params
    if step.op == "filter":
        return [p.column]
    if step.op in {"aggregate"}:
        return [p.column] if p.column else []
    if step.op in {"group_by", "share", "top_n", "compare_groups"}:
        cols = [p.dimension]
        if getattr(p, "metric", None):
            cols.append(p.metric)
        return cols
    if step.op in {"time_series", "period_compare"}:
        return [p.date_column] + ([p.metric] if p.metric else [])
    if step.op == "correlation":
        return [p.column_x, p.column_y]
    if step.op == "outlier_flag":
        return [p.column] + ([step.date_column] if getattr(step, "date_column", None) else [])
    return []


def validate_plan_against_fields(
    plan: AnalysisPlan, fields: list[FieldInfo]
) -> list[str]:
    """对已通过 schema 校验的方案做字典级合法性检查，返回中文错误原因列表。"""
    errors: list[str] = []
    available = {f.name: f for f in fields if not f.ignored}

    step_ids = [s.step_id for s in plan.steps]
    if len(step_ids) != len(set(step_ids)):
        errors.append("方案中存在重复的步骤 ID。")

    for step in plan.steps:
        if step.op not in OP_META:  # 理论上被 union 拦截，双保险
            errors.append(f"步骤 {step.step_id} 使用了未登记算子：{step.op}")
            continue

        # 引用列必须存在且未被忽略
        for col in step_columns(step):
            if col not in available:
                errors.append(
                    f"步骤 {step.step_id} 引用了不存在或已被忽略的字段：{col}"
                )

        p = step.params
        if step.op in TIME_OPS:
            f = available.get(p.date_column)
            if f and f.semantic_type != SemanticType.date:
                errors.append(
                    f"步骤 {step.step_id} 的时间字段「{p.date_column}」不是已确认的日期字段。"
                )
            if p.metric:
                _require_metric(errors, available, p.metric, step.step_id)

        if step.op in {"group_by", "share", "top_n", "compare_groups"}:
            d = available.get(p.dimension)
            if d and d.semantic_type not in DIMENSION_SEMANTICS:
                errors.append(
                    f"步骤 {step.step_id} 的分组字段「{p.dimension}」不是维度类型字段。"
                )
            if getattr(p, "metric", None):
                _require_metric(errors, available, p.metric, step.step_id)
            if step.op == "compare_groups" and len(p.members) < 2:
                errors.append(f"步骤 {step.step_id} 至少需要指定 2 个对比成员。")

        if step.op == "aggregate" and p.column:
            if p.func not in COUNT_FUNCS:
                _require_metric(errors, available, p.column, step.step_id)

        if step.op == "correlation":
            _require_metric(errors, available, p.column_x, step.step_id)
            _require_metric(errors, available, p.column_y, step.step_id)

        if step.op == "outlier_flag":
            _require_metric(errors, available, p.column, step.step_id)
            dc = getattr(step, "date_column", None)
            if dc:
                f = available.get(dc)
                if f and f.semantic_type != SemanticType.date:
                    errors.append(
                        f"步骤 {step.step_id} 的日期字段「{dc}」不是已确认的日期字段。"
                    )

        if step.op == "filter":
            if p.operator in _LIST_FILTER_OPS:
                if not isinstance(p.value, list) or len(p.value) == 0:
                    errors.append(
                        f"步骤 {step.step_id} 的筛选操作符 {p.operator.value} 需要列表类型的值。"
                    )
                if p.operator == FilterOperator.between and len(p.value) != 2:
                    errors.append(f"步骤 {step.step_id} 的 between 筛选需要恰好 2 个边界值。")
            elif p.value is None:
                errors.append(f"步骤 {step.step_id} 的筛选条件缺少比较值。")

    return errors


def _require_metric(
    errors: list[str], available: dict[str, FieldInfo], col: str, step_id: str
) -> None:
    f = available.get(col)
    if f and f.semantic_type != SemanticType.metric:
        errors.append(f"步骤 {step_id} 的指标字段「{col}」不是已确认的指标类型字段。")


def terminal_chart(plan: AnalysisPlan) -> ChartType:
    """末端算子决定图表；若末步是 filter（透传），以明细表呈现。"""
    last = plan.steps[-1]
    if last.op == "filter":
        return ChartType.table
    return CHART_BY_OP[last.op]
