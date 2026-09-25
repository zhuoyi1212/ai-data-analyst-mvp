"""10 个有限算子的 pandas 实现（FR-6 / 附录 A）。

红线：本模块是系统中**唯一**的数值来源。不 import 任何 LLM/HTTP 模块，
不接受代码/表达式字符串；所有输入均为强类型 params。
每个算子返回 OpResult（结果 DataFrame + 中间摘要 + 人读公式），供执行器台账留痕。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from app.schemas.plan import PlanStep

_VALUE_COL = "value"


class EngineError(Exception):
    """引擎计算错误：消息必须为中文，调用方直接阻断展示。"""


@dataclass
class OpResult:
    df: pd.DataFrame
    formula: str
    summary: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- 工具

_AGG_PANDAS: dict[str, str | Callable] = {
    "sum": "sum",
    "mean": "mean",
    "median": "median",
    "count": "count",
    "count_distinct": "nunique",
    "min": "min",
    "max": "max",
}

_AGG_LABEL = {
    "sum": "求和", "mean": "平均值", "median": "中位数", "count": "计数",
    "count_distinct": "去重计数", "min": "最小值", "max": "最大值",
}

_PANDAS_FREQ = {"day": "D", "week": "W", "month": "M", "quarter": "Q", "year": "Y"}
_PERIOD_LABEL_FMT = {
    "D": lambda p: str(p.start_time.date()),
    "W": lambda p: f"{p.start_time.date()} 起一周",
    "M": lambda p: f"{p.year}-{p.month:02d}",
    "Q": lambda p: f"{p.year}Q{((p.month - 1) // 3) + 1}",
    "Y": lambda p: f"{p.year}年",
}


def _require_column(df: pd.DataFrame, column: str) -> None:
    if column not in df.columns:
        raise EngineError(f"字段「{column}」在当前数据中不存在，无法执行计算。")


def _numeric_series(df: pd.DataFrame, column: str) -> pd.Series:
    _require_column(df, column)
    s = pd.to_numeric(df[column], errors="coerce")
    if s.notna().sum() == 0 and len(df) > 0:
        raise EngineError(f"指标字段「{column}」不是可计算的数值类型。")
    return s


def _agg(series: pd.Series, func: str) -> Any:
    try:
        if func == "count":
            return int(series.notna().sum())
        return float(getattr(series, _AGG_PANDAS[func])()) if isinstance(
            _AGG_PANDAS[func], str
        ) else float(_AGG_PANDAS[func](series))
    except (TypeError, ValueError) as exc:
        raise EngineError(f"无法对该字段执行「{_AGG_LABEL[func]}」计算。") from exc


def _metric_input(df: pd.DataFrame, column: str | None, func: str) -> tuple[pd.Series, str, int]:
    """返回（计算序列、口径列名、空值数）；count 允许空列（按行计数）。"""
    if func == "count" and not column:
        return pd.Series(df.index), "全部行", 0
    if not column:
        raise EngineError(f"聚合函数 {func} 必须指定指标字段。")
    if func in {"count", "count_distinct"}:
        _require_column(df, column)
        s = df[column]
    else:
        s = _numeric_series(df, column)
    return s, column, int(s.isna().sum())


# ---------------------------------------------------------------- 1. filter

def op_filter(df: pd.DataFrame, p: Any) -> OpResult:
    _require_column(df, p.column)
    s = df[p.column]
    op = p.operator
    value = p.value
    if op.value == "==":
        mask = s == value
    elif op.value == "!=":
        mask = s != value
    elif op.value == ">":
        mask = s > value
    elif op.value == ">=":
        mask = s >= value
    elif op.value == "<":
        mask = s < value
    elif op.value == "<=":
        mask = s <= value
    elif op.value == "in":
        if not isinstance(value, list):
            raise EngineError("in 筛选的值必须是列表。")
        mask = s.isin(value)
    elif op.value == "not_in":
        if not isinstance(value, list):
            raise EngineError("not_in 筛选的值必须是列表。")
        mask = ~s.isin(value)
    elif op.value == "contains":
        mask = s.astype("string").str.contains(str(value), na=False, regex=False)
    elif op.value in {"between", "date_between"}:
        if not isinstance(value, list) or len(value) != 2:
            raise EngineError("区间筛选需要 [下限, 上限] 两个边界值。")
        if op.value == "date_between":
            s2 = pd.to_datetime(s, errors="coerce")
            lo, hi = pd.to_datetime(value[0]), pd.to_datetime(value[1])
        else:
            numeric = pd.to_numeric(s, errors="coerce")
            if numeric.notna().any():
                s2, lo, hi = numeric, float(value[0]), float(value[1])
            else:
                s2, lo, hi = s, value[0], value[1]
        mask = (s2 >= lo) & (s2 <= hi)
    else:  # 理论不可达（枚举契约）
        raise EngineError(f"不支持的筛选操作符：{op}")
    out = df.loc[mask.fillna(False).to_numpy()].copy()
    formula = f"筛选：{p.column} {op.value} {value!r}（保留 {len(out)}/{len(df)} 行）"
    return OpResult(out, formula, {"input_rows": len(df), "output_rows": len(out)})


# ---------------------------------------------------------------- 2. aggregate

def op_aggregate(df: pd.DataFrame, p: Any) -> OpResult:
    s, label, null_count = _metric_input(df, p.column, p.func.value)
    value = _agg(s, p.func.value)
    out = pd.DataFrame({_VALUE_COL: [value]})
    formula = f"{_AGG_LABEL[p.func.value]}（{label}）"
    return OpResult(out, formula, {"value": value, "input_rows": len(df),
                                   "null_excluded": null_count})


# ---------------------------------------------------------------- 3/4/5 分组族

def _group_values(df: pd.DataFrame, dimension: str, metric: str | None, func: str) -> pd.Series:
    _require_column(df, dimension)
    if func == "count" and not metric:
        return df.groupby(dimension, dropna=False).size()
    s, _, _ = _metric_input(df, metric, func)
    work = pd.DataFrame({dimension: df[dimension], "_m": s})
    if func in {"count", "count_distinct"}:
        grouped = work.groupby(dimension, dropna=False)["_m"].agg(_AGG_PANDAS[func])
    else:
        grouped = work.groupby(dimension, dropna=False)["_m"].agg(_AGG_PANDAS[func])
    return grouped


def op_group_by(df: pd.DataFrame, p: Any) -> OpResult:
    grouped = _group_values(df, p.dimension, p.metric, p.func.value)
    out = grouped.reset_index().rename(columns={grouped.name or 0: _VALUE_COL})
    out = out.sort_values(_VALUE_COL, ascending=(p.order.value == "asc"), na_position="last")
    if p.limit:
        out = out.head(p.limit)
    out = out.reset_index(drop=True)
    formula = f"按「{p.dimension}」分组，对「{p.metric or '行'}」{_AGG_LABEL[p.func.value]}"
    return OpResult(out, formula, {"groups": len(out)})


def op_share(df: pd.DataFrame, p: Any) -> OpResult:
    grouped = _group_values(df, p.dimension, p.metric, p.func.value)
    total = float(grouped.sum())
    if total == 0:
        raise EngineError("占比分母为 0，无法计算占比。")
    out = grouped.reset_index().rename(columns={grouped.name or 0: _VALUE_COL})
    out["share"] = out[_VALUE_COL] / total
    out = out.sort_values(_VALUE_COL, ascending=False).reset_index(drop=True)
    formula = f"「{p.metric or '行数'}」按「{p.dimension}」拆分的占比（各分组 ÷ 合计 {total:g}）"
    return OpResult(out, formula, {"total": total, "share_sum": float(out["share"].sum())})


def op_top_n(df: pd.DataFrame, p: Any) -> OpResult:
    grouped = _group_values(df, p.dimension, p.metric, p.func.value)
    out = grouped.reset_index().rename(columns={grouped.name or 0: _VALUE_COL})
    out = out.sort_values(_VALUE_COL, ascending=(p.order.value == "asc"),
                          na_position="last").head(p.n).reset_index(drop=True)
    formula = f"按「{p.dimension}」对「{p.metric or '行'}」{_AGG_LABEL[p.func.value]}取 Top {p.n}"
    return OpResult(out, formula, {"n": len(out)})


# ---------------------------------------------------------------- 6/7 时间族

def _period_series(df: pd.DataFrame, date_column: str) -> tuple[pd.DataFrame, int]:
    _require_column(df, date_column)
    dates = pd.to_datetime(df[date_column], errors="coerce")
    nat = int(dates.isna().sum())
    work = df.loc[dates.notna()].copy()
    work[date_column] = dates.loc[dates.notna()]
    return work, nat


def op_time_series(df: pd.DataFrame, p: Any) -> OpResult:
    work, nat = _period_series(df, p.date_column)
    freq = _PANDAS_FREQ[p.granularity.value]
    periods = work[p.date_column].dt.to_period(freq)
    null_metric = 0
    if p.func.value == "count" and not p.metric:
        grouped = work.groupby(periods).size()
    else:
        s, _, null_metric = _metric_input(work, p.metric, p.func.value)
        grouped = s.groupby(periods.loc[s.index]).agg(_AGG_PANDAS[p.func.value]) \
            if p.func.value not in {"count", "count_distinct"} else \
            work.assign(_m=s).groupby(periods)["_m"].agg(_AGG_PANDAS[p.func.value])
    full_index = pd.period_range(grouped.index.min(), grouped.index.max(), freq=freq)
    grouped = grouped.reindex(full_index)
    out = pd.DataFrame({
        p.date_column: [per.start_time for per in grouped.index],
        _VALUE_COL: grouped.to_numpy(),
    })
    formula = (f"按{ {'day':'日','week':'周','month':'月','quarter':'季','year':'年'}[p.granularity.value] }"
               f"聚合「{p.metric or '行数'}」的{_AGG_LABEL[p.func.value]}")
    return OpResult(out, formula, {"periods": len(out), "null_dates_excluded": nat,
                                   "null_excluded": null_metric})


def op_period_compare(df: pd.DataFrame, p: Any) -> OpResult:
    work, nat = _period_series(df, p.date_column)
    freq = {"yoy": "Y", "mom": "M", "wow": "W"}[p.period.value]
    label_map = _PERIOD_LABEL_FMT[freq]
    if p.func.value == "count" and not p.metric:
        grouped = work.groupby(work[p.date_column].dt.to_period(freq)).size()
    else:
        s, _, _ = _metric_input(work, p.metric, p.func.value)
        tmp = work.assign(_m=s)
        grouped = tmp.groupby(tmp[p.date_column].dt.to_period(freq))["_m"].agg(
            _AGG_PANDAS[p.func.value])
    if len(grouped) < 1:
        raise EngineError("日期字段中没有可用于同环比的有效时间数据。")
    current_period = grouped.index.max()
    prev_period = current_period - 1
    cur = grouped.get(current_period, np.nan)
    if prev_period not in grouped.index:
        raise EngineError(
            f"缺少上一周期（{label_map(prev_period)}）的数据，无法计算"
            f"{'同比' if p.period.value == 'yoy' else '环比'}。"
        )
    prev = grouped.loc[prev_period]
    growth = None if prev == 0 else float((cur - prev) / prev * 100)
    if prev == 0:
        raise EngineError("上一周期数值为 0，增长率无定义，无法进行同环比对比。")
    out = pd.DataFrame({
        "period": [label_map(current_period), label_map(prev_period)],
        _VALUE_COL: [float(cur), float(prev)],
    })
    formula = (f"{label_map(current_period)} vs {label_map(prev_period)}"
               f"「{p.metric or '行数'}」{_AGG_LABEL[p.func.value]}及变化率")
    return OpResult(out, formula, {
        "current_value": float(cur), "previous_value": float(prev),
        "growth_pct": growth, "null_dates_excluded": nat,
    })


# ---------------------------------------------------------------- 8. compare_groups

def op_compare_groups(df: pd.DataFrame, p: Any) -> OpResult:
    grouped = _group_values(df, p.dimension, p.metric, p.func.value)
    present = set(grouped.index.dropna().tolist())
    missing = [m for m in p.members if m not in present]
    if missing:
        valid = "、".join(map(str, list(present)[:10]))
        raise EngineError(
            f"对比成员在字段「{p.dimension}」中不存在：{'、'.join(missing)}。"
            f"现有取值示例：{valid}"
        )
    values = [float(grouped.loc[m]) for m in p.members]
    out = pd.DataFrame({p.dimension: list(p.members), _VALUE_COL: values})
    formula = f"按「{p.dimension}」对比 {('、'.join(p.members))} 的「{p.metric or '行数'}」{_AGG_LABEL[p.func.value]}"
    return OpResult(out, formula, {"members": p.members, "values": values})


# ---------------------------------------------------------------- 9. correlation

def op_correlation(df: pd.DataFrame, p: Any) -> OpResult:
    sx = _numeric_series(df, p.column_x)
    sy = _numeric_series(df, p.column_y)
    pairs = pd.DataFrame({"x": sx, "y": sy}).dropna()
    if len(pairs) < 3:
        raise EngineError("有效样本不足 3 对，无法进行相关分析。")
    method = p.method.value
    r = float(pairs["x"].corr(pairs["y"], method=method))
    if np.isnan(r):
        raise EngineError("相关系数无法计算（可能存在常量列）。")
    out = pairs.rename(columns={"x": p.column_x, "y": p.column_y}).reset_index(drop=True)
    formula = f"{method} 相关系数（{p.column_x}，{p.column_y}），n={len(pairs)}"
    return OpResult(out, formula, {"coefficient": r, "n": len(pairs), "method": method})


# ---------------------------------------------------------------- 10. outlier_flag

def op_outlier_flag(df: pd.DataFrame, p: Any) -> OpResult:
    s = _numeric_series(df, p.column)
    valid = s.dropna()
    if len(valid) < 4:
        raise EngineError("有效数据不足 4 行，无法进行离群检测。")
    if p.method.value == "iqr":
        q1, q3 = valid.quantile(0.25), valid.quantile(0.75)
        iqr = q3 - q1
        lo, hi = q1 - p.threshold * iqr, q3 + p.threshold * iqr
        mask_num = (s < lo) | (s > hi)
        rule = f"IQR（{p.threshold:g} 倍），下界 {lo:g}，上界 {hi:g}"
    else:
        mu, sigma = valid.mean(), valid.std(ddof=0)
        if sigma == 0:
            raise EngineError("指标标准差为 0，z-score 无法判定离群。")
        z = (s - mu) / sigma
        lo, hi = mu - p.threshold * sigma, mu + p.threshold * sigma
        mask_num = z.abs() > p.threshold
        rule = f"z-score（阈值 {p.threshold:g}），下界 {lo:g}，上界 {hi:g}"
    mask = (mask_num.fillna(False)).to_numpy()
    out = df.copy()
    out["is_outlier"] = mask
    count = int(mask.sum())
    formula = f"对「{p.column}」做{rule}，标记 {count} 个离群点"
    return OpResult(out, formula, {
        "lower_bound": float(lo), "upper_bound": float(hi),
        "outlier_count": count, "outlier_rate": count / len(df) if len(df) else 0.0,
        "method": p.method.value,
    })


_DISPATCH: dict[str, Callable[[pd.DataFrame, Any], OpResult]] = {
    "filter": op_filter,
    "aggregate": op_aggregate,
    "group_by": op_group_by,
    "share": op_share,
    "top_n": op_top_n,
    "time_series": op_time_series,
    "period_compare": op_period_compare,
    "compare_groups": op_compare_groups,
    "correlation": op_correlation,
    "outlier_flag": op_outlier_flag,
}


def run_step(df: pd.DataFrame, step: PlanStep) -> OpResult:
    """执行单个已通过契约校验的步骤。"""
    handler = _DISPATCH.get(step.op)
    if handler is None:  # 双保险：目录外算子永不执行
        raise EngineError(f"未登记的算子不允许执行：{step.op}")
    return handler(df, step.params)
