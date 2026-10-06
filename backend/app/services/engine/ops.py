"""有限算子的 pandas 实现（FR-6 / 附录 A；Phase 2 起共 11 个，含 derive_ratio）。

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

def _last_valid(series: pd.Series) -> float:
    """期末/末点快照：窗口内最后一个非空观测；全空返回 NaN。"""
    valid = series.dropna()
    return float(valid.iloc[-1]) if len(valid) else float("nan")


_AGG_PANDAS: dict[str, str | Callable] = {
    "sum": "sum",
    "mean": "mean",
    "median": "median",
    "count": "count",
    "count_distinct": "nunique",
    "min": "min",
    "max": "max",
    "last": _last_valid,
}

_AGG_LABEL = {
    "sum": "求和", "mean": "平均值", "median": "中位数", "count": "计数",
    "count_distinct": "去重计数", "min": "最小值", "max": "最大值",
    "last": "期末值",
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
    """同/环比（T05）：窗口对齐、残缺期等长 MTD、负基数变化口径全部走
    period_compare 共享模块，算子与 Dashboard KPI/Finding 同一实现。"""
    from app.services.period_compare import PeriodCompareError, period_comparison

    try:
        summary = period_comparison(
            df, p.date_column, p.metric, p.func.value, p.period.value
        )
    except PeriodCompareError as exc:
        raise EngineError(str(exc)) from exc
    if summary is None:
        raise EngineError("日期字段中没有可用于同环比的有效时间数据。")

    out = pd.DataFrame({
        "period": [summary["current_label"], summary["compare_label"]],
        _VALUE_COL: [summary["current_value"], summary["previous_value"]],
    })
    formula = (
        f"{summary['comparison']}：{summary['current_label']} vs "
        f"{summary['compare_label']}，「{p.metric or '行数'}」"
        f"{_AGG_LABEL[p.func.value]}"
        + ("（残缺期，按等长窗口比较）" if summary["completeness"] != "complete" else "")
    )
    summary["null_dates_excluded"] = int(
        pd.to_datetime(df[p.date_column], errors="coerce").isna().sum()
    )
    return OpResult(out, formula, summary)


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

# T10：相关结论的稳定性门槛（自动发现/报告门禁引用；算子本身仍按 n≥3 给原始值）。
MIN_CORRELATION_PAIRS = 8   # 允许作为「关系结论」展示的最小有效对数
LOO_STRONG_R = 0.70         # 全样本 |r| 达到此值才算「看起来很强」
LOO_FRAGILE_R = 0.50        # 剔除影响最大的单点后 |r| 跌破此值 → 关系不稳
LOO_MIN_DELTA = 0.20        # 且该单点造成的 |Δr| 至少这么大才判定为单点主导
SPEARMAN_EXACT_LOO_MAX_N = 200  # 此 n 以下 Spearman 逐点精确重排秩；以上走全样本秩近似


def _pearson_r(x: pd.Series, y: pd.Series) -> float:
    """纯 numpy Pearson，不依赖 scipy（锁文件全新环境也确定可用）。"""
    xa = x.to_numpy(dtype=float)
    ya = y.to_numpy(dtype=float)
    xc = xa - xa.mean()
    yc = ya - ya.mean()
    denom = float(np.sqrt((xc ** 2).sum() * (yc ** 2).sum()))
    if denom == 0:
        return float("nan")
    return float((xc * yc).sum() / denom)


def _loo_r_pearson(xa: np.ndarray, ya: np.ndarray) -> np.ndarray:
    """留一 Pearson r 数组（充分统计量恒等式，精确且 O(n)）。

    Sxx_{-i} = Sxx − n/(n−1)·(x_i−x̄)²；Sxy 同理。剔除后成常量列的点给 nan。
    """
    n = len(xa)
    xc = xa - xa.mean()
    yc = ya - ya.mean()
    sxx = float((xc ** 2).sum())
    syy = float((yc ** 2).sum())
    sxy = float((xc * yc).sum())
    k = n / (n - 1)
    sxx_i = sxx - k * xc ** 2
    syy_i = syy - k * yc ** 2
    sxy_i = sxy - k * xc * yc
    denom = np.sqrt(sxx_i * syy_i)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denom > 0, sxy_i / denom, np.nan)


def _loo_r_spearman(pairs: pd.DataFrame) -> tuple[np.ndarray, bool]:
    """留一 Spearman。n≤200 逐点剔除后重排平均秩（精确）；否则在全样本秩上
    走充分统计量恒等式（近似：剔除一个结只引起 1/n 量级的秩位移）。
    """
    n = len(pairs)
    if n <= SPEARMAN_EXACT_LOO_MAX_N:
        out = np.empty(n)
        for i in range(n):
            ranked = pairs.drop(index=pairs.index[i]).rank(method="average")
            out[i] = _pearson_r(ranked["x"], ranked["y"])
        return out, True
    ranked = pairs[["x", "y"]].rank(method="average")
    return _loo_r_pearson(
        ranked["x"].to_numpy(dtype=float),
        ranked["y"].to_numpy(dtype=float),
    ), False


def _correlation_sensitivity(
    method: str, pairs: pd.DataFrame, r: float
) -> dict:
    """留一（leave-one-out）稳定性检验：单个异常点能否制造/摧毁该相关性。

    状态：
    - single_point_driven：全样本 |r|≥0.70、剔除最敏感单点后 |r|<0.50 且 |Δr|≥0.20
      —— 自动链路必须拒绝作为关系结论（T10 假相关反例）；
    - insufficient_n：有效对数 < 8，不做 LOO，自动链路同样拒绝（小样本巧合）；
    - ok：通过留一检验（或 inconclusive：全部剔除都导致常量列，无法判定）。
    """
    n = len(pairs)
    base = {
        "kind": "leave_one_out",
        "min_pairs_for_claim": MIN_CORRELATION_PAIRS,
        "thresholds": {
            "strong_r": LOO_STRONG_R, "fragile_r": LOO_FRAGILE_R,
            "min_delta": LOO_MIN_DELTA,
        },
    }
    if n < MIN_CORRELATION_PAIRS:
        return {
            **base, "status": "insufficient_n", "loo_exact": None,
            "max_abs_delta_r": None, "most_influential_index": None,
            "r_without_most_influential": None,
            "note": (
                f"有效样本仅 {n} 对（少于 {MIN_CORRELATION_PAIRS} 对），"
                "小样本相关系数极易偶然接近 ±1，不做留一稳定性检验，"
                "也不得作为关系结论。"),
        }

    if method == "spearman":
        r_loo, exact = _loo_r_spearman(pairs)
    else:
        r_loo = _loo_r_pearson(
            pairs["x"].to_numpy(dtype=float),
            pairs["y"].to_numpy(dtype=float),
        )
        exact = True

    valid = ~np.isnan(r_loo)
    if not valid.any():
        return {
            **base, "status": "inconclusive", "loo_exact": exact,
            "max_abs_delta_r": None, "most_influential_index": None,
            "r_without_most_influential": None,
            "note": "剔除任意一对后均出现常量列，留一稳定性无法判定。",
        }

    deltas = np.abs(r - r_loo)
    deltas[~valid] = -1.0
    idx = int(np.argmax(deltas))
    max_delta = float(abs(r - r_loo[idx]))
    r_without = float(r_loo[idx])
    driven = (
        abs(r) >= LOO_STRONG_R
        and abs(r_without) < LOO_FRAGILE_R
        and max_delta >= LOO_MIN_DELTA
    )
    status = "single_point_driven" if driven else "ok"
    if driven:
        note = (
            f"全样本 r={r:.2f}（n={n}）看似显著，但剔除影响最大的第 {idx + 1} 对"
            f"数据后 r={r_without:.2f}（|Δr|={max_delta:.2f}），相关性由单个异常点"
            "主导，不构成稳定关系，自动分析不得据此下关系结论；请先在异常分析中"
            "复核该数据点。"
        )
    else:
        note = (
            f"留一检验通过：剔除任意单点后 r 最大变化 {max_delta:.2f}，"
            "相关性不依赖单个数据点。"
            + ("" if exact else "（大样本 Spearman 基于全样本秩近似）")
        )
    return {
        **base, "status": status, "loo_exact": exact,
        "max_abs_delta_r": round(max_delta, 6),
        "most_influential_index": idx,
        "r_without_most_influential": round(r_without, 6),
        "note": note,
    }


def op_correlation(df: pd.DataFrame, p: Any) -> OpResult:
    sx = _numeric_series(df, p.column_x)
    sy = _numeric_series(df, p.column_y)
    pairs = pd.DataFrame({"x": sx, "y": sy}).dropna()
    if len(pairs) < 3:
        raise EngineError("有效样本不足 3 对，无法进行相关分析。")
    method = p.method.value
    if method == "spearman":
        # Spearman = 秩的 Pearson：先取平均秩（结处理与 scipy.stats.spearmanr 一致），
        # 再用纯 numpy Pearson，避免锁文件环境缺 scipy 时 ModuleNotFoundError（反例 10）。
        ranked = pairs[["x", "y"]].rank(method="average")
        r = _pearson_r(ranked["x"], ranked["y"])
    else:
        r = _pearson_r(pairs["x"], pairs["y"])
    if np.isnan(r):
        raise EngineError("相关系数无法计算（可能存在常量列）。")
    sensitivity = _correlation_sensitivity(method, pairs, r)
    out = pairs.rename(columns={"x": p.column_x, "y": p.column_y}).reset_index(drop=True)
    formula = f"{method} 相关系数（{p.column_x}，{p.column_y}），n={len(pairs)}"
    return OpResult(
        out, formula,
        {"coefficient": r, "n": len(pairs), "method": method,
         "sensitivity": sensitivity},
    )


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


# ---------------------------------------------------------------- 11. derive_ratio

def _ratio_frame(
    num: pd.Series, den: pd.Series
) -> tuple[float, float, float | None]:
    """整体 ratio-of-sums；分母为 0/空时比率为 None。"""
    total_n = float(num.sum())
    total_d = float(den.sum())
    ratio = None if total_d == 0 or np.isnan(total_d) else total_n / total_d
    return total_n, total_d, ratio


def op_derive_ratio(df: pd.DataFrame, p: Any) -> OpResult:
    """确定性派生比率：Σnumerator / Σdenominator（先聚合后相除）。

    - 无轴：整体单值（KPI 利润率等）；
    - dimension：每个维度成员的比率（分母为 0/空的成员输出 null，不抛断整批）；
    - date_column + granularity：按时间周期的比率序列。
    严禁对行级 numerator/denominator 先求比率再平均（口径错误）。
    """
    num = _numeric_series(df, p.numerator)
    den = _numeric_series(df, p.denominator)
    label = f"「{p.numerator}」÷「{p.denominator}」"

    if p.dimension:
        _require_column(df, p.dimension)
        work = pd.DataFrame({p.dimension: df[p.dimension].to_numpy(),
                             "_n": num.to_numpy(), "_d": den.to_numpy()})
        g = work.groupby(p.dimension, dropna=False)[["_n", "_d"]].sum(min_count=1)
        g[_VALUE_COL] = g["_n"] / g["_d"].replace({0: np.nan})
        out = (g.reset_index()[[p.dimension, _VALUE_COL]]
               .sort_values(_VALUE_COL, ascending=False, na_position="last")
               .reset_index(drop=True))
        tn, td, overall = _ratio_frame(num, den)
        formula = f"{label} 按「{p.dimension}」先聚合后相除的比率"
        return OpResult(out, formula, {
            "dimension": p.dimension, "groups": len(out),
            "null_member_count": int(g[_VALUE_COL].isna().sum()),
            "total_numerator": tn, "total_denominator": td, "overall_ratio": overall,
        })

    if p.date_column:
        work, nat = _period_series(df, p.date_column)
        freq = _PANDAS_FREQ[p.granularity.value]
        sn = pd.to_numeric(work[p.numerator], errors="coerce")
        sd = pd.to_numeric(work[p.denominator], errors="coerce")
        tmp = pd.DataFrame({"_p": work[p.date_column].dt.to_period(freq),
                            "_n": sn.to_numpy(), "_d": sd.to_numpy()})
        g = tmp.groupby("_p")[["_n", "_d"]].sum(min_count=1)
        g[_VALUE_COL] = g["_n"] / g["_d"].replace({0: np.nan})
        full_index = pd.period_range(g.index.min(), g.index.max(), freq=freq)
        g = g.reindex(full_index)
        out = pd.DataFrame({
            p.date_column: [per.start_time for per in g.index],
            _VALUE_COL: g[_VALUE_COL].to_numpy(),
        })
        formula = (f"{label} 按"
                   f"{ {'day':'日','week':'周','month':'月','quarter':'季','year':'年'}[p.granularity.value] }"
                   f"先聚合后相除的比率")
        return OpResult(out, formula, {
            "date_column": p.date_column, "periods": len(out),
            "null_period_count": int(g[_VALUE_COL].isna().sum()),
            "null_dates_excluded": nat,
        })

    tn, td, ratio = _ratio_frame(num, den)
    out = pd.DataFrame({_VALUE_COL: [ratio]})
    formula = f"{label} 整体比率（合计 ÷ 合计）"
    return OpResult(out, formula, {
        "value": ratio, "total_numerator": tn, "total_denominator": td,
        "denominator_zero": ratio is None,
    })


# ----------------------------------------------------- 12/13 T08 跨指标信号


def _window_group_values(
    df: pd.DataFrame, date_column: str, dimension: str,
    metric: str, func: str,
    start: pd.Timestamp, end: pd.Timestamp,
) -> pd.Series:
    """窗口内按维度分组的指标聚合（T08；窗口对齐复用 T05 日历口径）。"""
    dates = pd.to_datetime(df[date_column], errors="coerce")
    mask = (dates >= start) & (dates <= end)
    sub = df.loc[mask]
    if sub.empty:
        return pd.Series(dtype=float)
    _require_column(sub, dimension)
    if func == "count":
        return sub.groupby(dimension, dropna=False).size().astype(float)
    if func == "count_distinct":
        _require_column(sub, metric)
        return (sub.groupby(dimension, dropna=False)[metric]
                .nunique().astype(float))
    s, _, _ = _metric_input(sub, metric, func)
    work = pd.DataFrame({dimension: sub[dimension].to_numpy(), "_m": s.to_numpy()})
    return work.groupby(dimension, dropna=False)["_m"].agg(
        _AGG_PANDAS[func]
    ).astype(float)


def _period_windows(
    df: pd.DataFrame, date_column: str, mode: str
) -> tuple:
    """返回 (cur_start, cur_end, base_start, base_end, spec)；数据不足即 EngineError。"""
    from app.services.period_compare import build_period_spec

    dates = pd.to_datetime(df[date_column], errors="coerce").dropna()
    if dates.empty:
        raise EngineError("日期字段中没有可用于比较的有效时间数据。")
    spec = build_period_spec(dates.max(), mode, dates)
    return (
        pd.Timestamp(spec.current_start), pd.Timestamp(spec.current_end),
        pd.Timestamp(spec.compare_start), pd.Timestamp(spec.compare_end),
        spec,
    )


def op_contribution(df: pd.DataFrame, p: Any) -> OpResult:
    """分组变化贡献（加法会计恒等式，T08）。

    ΔT = T1 − T0 = Σ_i (v1_i − v0_i) + unexplained
    - sum 口径：成员为窗口内无行时按 0；恒等式精确成立，unexplained≈0；
    - count_distinct 口径：unexplained 显式承载键跨组重复/消失效应；
    - contribution_share = δ_i / |ΔT|（对「变化」的贡献，不是成员值占总
      利润的组成比；总利润为负时仍有效）；|ΔT|≈0 时为 null。
    贡献是会计拆解，文案不称因果。
    """
    from app.services.period_compare import _window_aggregate

    cur_s, cur_e, base_s, base_e, spec = _period_windows(
        df, p.date_column, p.period.value
    )
    g_cur = _window_group_values(
        df, p.date_column, p.dimension, p.metric, p.func.value, cur_s, cur_e
    )
    g_base = _window_group_values(
        df, p.date_column, p.dimension, p.metric, p.func.value, base_s, base_e
    )
    if g_cur.empty and g_base.empty:
        raise EngineError("当前期与对比期窗口内均无数据，无法分解变化。")

    members = sorted(
        set(g_base.index).union(g_cur.index), key=lambda x: str(x)
    )
    base_vals = [float(g_base.get(m, 0.0)) for m in members]
    cur_vals = [float(g_cur.get(m, 0.0)) for m in members]
    deltas = [c - b for c, b in zip(cur_vals, base_vals)]

    total_base = _window_aggregate(
        df, p.date_column, p.metric, p.func.value, base_s, base_e
    )
    total_cur = _window_aggregate(
        df, p.date_column, p.metric, p.func.value, cur_s, cur_e
    )
    total_base = float(total_base) if total_base is not None else 0.0
    total_cur = float(total_cur) if total_cur is not None else 0.0
    total_delta = total_cur - total_base
    explained = float(sum(deltas))
    unexplained = total_delta - explained
    shares = [
        (d / total_delta if abs(total_delta) > 1e-9 else None) for d in deltas
    ]

    out = pd.DataFrame({
        p.dimension: list(members),
        "base_value": base_vals, "current_value": cur_vals,
        "delta": deltas, "contribution_share": shares,
    })
    out = out.reindex(
        out["delta"].abs().sort_values(ascending=False).index
    ).reset_index(drop=True)

    partial_note = "（当期未结束，按截至日等长窗口比较）" \
        if spec.completeness != "complete" else ""
    formula = (
        f"「{p.metric}」按「{p.dimension}」的变化贡献"
        f"（{spec.compare_start}~{spec.compare_end} → "
        f"{spec.current_start}~{spec.current_end}{partial_note}，{p.func.value} 口径）"
    )
    return OpResult(out, formula, {
        "total_base_value": total_base,
        "total_current_value": total_cur,
        "total_delta": total_delta,
        "explained_delta": explained,
        "unexplained_delta": unexplained,
        # 回算残差：total_delta − explained − unexplained，恒为 0（契约留痕）
        "identity_residual": total_delta - explained - unexplained,
        "current_label": f"{spec.current_start} ~ {spec.current_end}",
        "base_label": f"{spec.compare_start} ~ {spec.compare_end}",
        "completeness": spec.completeness,
        "func": p.func.value,
        "members": len(members),
    })


def op_rate_decomposition(df: pd.DataFrame, p: Any) -> OpResult:
    """率的结构变化分解（shift-share，T08）。

    R1 − R0 = Σ w0(r1−r0) + Σ r0(w1−w0) + Σ(r1−r0)(w1−w0)
            = within（各组率变化）+ mix（结构权重变化）+ interaction
    - 分子分母均为窗口内 Σ 先聚合后相除（与 derive_ratio 同口径）；
    - 成员缺失窗口的率以该窗口整体率作为反事实填充，权重为 0，
      恒等式仍严格成立；原始率列保留 null。
    """
    cur_s, cur_e, base_s, base_e, spec = _period_windows(
        df, p.date_column, p.period.value
    )
    n_base = _window_group_values(
        df, p.date_column, p.dimension, p.numerator, "sum", base_s, base_e
    )
    d_base = _window_group_values(
        df, p.date_column, p.dimension, p.denominator, "sum", base_s, base_e
    )
    n_cur = _window_group_values(
        df, p.date_column, p.dimension, p.numerator, "sum", cur_s, cur_e
    )
    d_cur = _window_group_values(
        df, p.date_column, p.dimension, p.denominator, "sum", cur_s, cur_e
    )
    if d_base.empty and d_cur.empty:
        raise EngineError("当前期与对比期窗口内均无分母数据，无法分解率变化。")

    D0 = float(d_base.sum())
    D1 = float(d_cur.sum())
    if D0 == 0 or D1 == 0:
        raise EngineError(
            f"分母合计为 0（基期 {D0:g} / 当前 {D1:g}），率不可分解。"
        )
    N0 = float(n_base.sum())
    N1 = float(n_cur.sum())
    R0, R1 = N0 / D0, N1 / D1

    members = sorted(
        set(d_base.index).union(d_cur.index), key=lambda x: str(x)
    )
    rows = []
    for m in members:
        nb = float(n_base.get(m, 0.0))
        db = float(d_base.get(m, 0.0))
        nc = float(n_cur.get(m, 0.0))
        dc = float(d_cur.get(m, 0.0))
        rb = (nb / db) if db != 0 else None
        rc = (nc / dc) if dc != 0 else None
        wb, wc = db / D0, dc / D1
        rb_f = rb if rb is not None else R0
        rc_f = rc if rc is not None else R1
        within = wb * (rc_f - rb_f)
        mix = rb_f * (wc - wb)
        interaction = (rc_f - rb_f) * (wc - wb)
        rows.append((m, rb, rc, wb, wc, within, mix, interaction))

    out = pd.DataFrame(rows, columns=[
        p.dimension, "base_rate", "current_rate", "base_weight",
        "current_weight", "within", "mix", "interaction",
    ])
    out = out.reindex(
        (out["within"] + out["mix"] + out["interaction"])
        .abs().sort_values(ascending=False).index
    ).reset_index(drop=True)

    within_eff = float(out["within"].sum())
    mix_eff = float(out["mix"].sum())
    inter_eff = float(out["interaction"].sum())
    change = R1 - R0
    partial_note = "（当期未结束，按截至日等长窗口比较）" \
        if spec.completeness != "complete" else ""
    formula = (
        f"「{p.numerator}/{p.denominator}」按「{p.dimension}」的率结构变化"
        f"（within+mix+interaction{partial_note}）"
    )
    return OpResult(out, formula, {
        "base_overall_rate": R0,
        "current_overall_rate": R1,
        "change_pp": change * 100,
        "within_effect_pp": within_eff * 100,
        "mix_effect_pp": mix_eff * 100,
        "interaction_effect_pp": inter_eff * 100,
        "identity_residual": change - within_eff - mix_eff - inter_eff,
        "total_base_numerator": N0, "total_base_denominator": D0,
        "total_current_numerator": N1, "total_current_denominator": D1,
        "current_label": f"{spec.current_start} ~ {spec.current_end}",
        "base_label": f"{spec.compare_start} ~ {spec.compare_end}",
        "completeness": spec.completeness,
        "members": len(members),
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
    "derive_ratio": op_derive_ratio,
    "contribution": op_contribution,
    "rate_decomposition": op_rate_decomposition,
}


def run_step(df: pd.DataFrame, step: PlanStep) -> OpResult:
    """执行单个已通过契约校验的步骤。"""
    handler = _DISPATCH.get(step.op)
    if handler is None:  # 双保险：目录外算子永不执行
        raise EngineError(f"未登记的算子不允许执行：{step.op}")
    return handler(df, step.params)
