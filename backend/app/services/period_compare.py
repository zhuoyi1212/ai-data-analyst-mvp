"""可比时间窗口与变化口径（P0 T05，算子与合成器共享的唯一实现）。

规则（执行清单 T05）：
- 分离「时间粒度」与「比较模式」：mom/wow/yoy 都是确定的窗口对齐，不再用
  年度累计冒充同比（反例 7）；
- 残缺周期：截至日未到期末 → partial，MTD 只能与等长同期窗口比较（反例 5）；
  自然期已结束但中间缺日 → unknown；两者都不触发完整周期告警；
- 变化口径：正基数才展示增长率；负基数/跨零/零基数只给绝对差额 + 状态
  （扭亏为盈/由盈转亏/亏损收窄/亏损扩大）（反例 6）；率的变化用百分点。
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from app.schemas.period import ComparisonMode, PeriodSpec

NEAR_ZERO = 1e-9


class PeriodCompareError(Exception):
    """窗口/数据不足以完成同环比；消息为中文。"""


# ----------------------------------------------------------------- 变化口径

def compare_change(
    current: float | None, previous: float | None, *, is_rate: bool = False
) -> dict[str, Any]:
    """返回 delta/growth_pct/change_pp/status。任何分支都不伪造增长率。"""
    out: dict[str, Any] = {
        "current_value": current, "previous_value": previous,
        "delta": None, "growth_pct": None, "change_pp": None,
        "status": "", "is_rate": is_rate,
    }
    if current is None or previous is None or (
        isinstance(current, float) and np.isnan(current)
    ) or (isinstance(previous, float) and np.isnan(previous)):
        out["status"] = "对比期数据不足，不计算变化"
        return out

    delta = float(current) - float(previous)
    out["delta"] = delta
    if is_rate:
        # 率值（小数存储）的变化用百分点，不算相对增长率
        out["change_pp"] = delta * 100

    if previous > NEAR_ZERO:
        out["growth_pct"] = delta / previous * 100
        return out
    if previous == 0 or abs(previous) <= NEAR_ZERO:
        out["status"] = "基数为零，不展示增长率"
        return out

    # previous < 0：负基数区间
    if current >= 0:
        out["status"] = "扭亏为盈"
    elif current < 0 and current > previous:
        out["status"] = "亏损收窄"
    elif current < 0 and current < previous:
        out["status"] = "亏损扩大"
    else:
        out["status"] = "亏损持平"
    return out


# ----------------------------------------------------------------- 窗口

def _observed_days(dates_norm: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> int:
    mask = (dates_norm >= start) & (dates_norm <= end)
    return int(dates_norm[mask].dt.normalize().nunique())


def build_period_spec(as_of: pd.Timestamp, mode: ComparisonMode,
                      dates_norm: pd.Series) -> PeriodSpec:
    """以 max(event_time) 为截至线索构造对齐窗口；闰年/大小月按日历裁剪。"""
    as_of = pd.Timestamp(as_of).normalize()

    if mode == "mom":
        cur_period = as_of.to_period("M")
        cur_start = cur_period.start_time
        month_days = cur_period.days_in_month
        naturally_ended = as_of.day >= month_days
        cur_end = cur_period.end_time if naturally_ended else as_of
        prev_period = cur_period - 1
        cmp_start = prev_period.start_time
        window_len = month_days if naturally_ended else min(as_of.day, prev_period.days_in_month)
        cmp_end = cmp_start + pd.Timedelta(days=window_len - 1)
        gran = "month"
    elif mode == "wow":
        cur_start = (as_of - pd.Timedelta(days=as_of.weekday())).normalize()
        elapsed = (as_of - cur_start).days + 1
        naturally_ended = elapsed >= 7
        cur_end = cur_start + pd.Timedelta(days=6) if naturally_ended else as_of
        cmp_start = cur_start - pd.Timedelta(days=7)
        cmp_end = cmp_start + pd.Timedelta(days=elapsed - 1)
        gran = "week"
    elif mode == "yoy":
        cur_period = as_of.to_period("M")
        cur_start = cur_period.start_time
        month_days = cur_period.days_in_month
        naturally_ended = as_of.day >= month_days
        cur_end = cur_period.end_time if naturally_ended else as_of
        cmp_period = pd.Period(year=cur_period.year - 1, month=cur_period.month, freq="M")
        cmp_start = cmp_period.start_time
        window_len = month_days if naturally_ended else min(as_of.day, cmp_period.days_in_month)
        cmp_end = cmp_start + pd.Timedelta(days=window_len - 1)
        gran = "month"
    else:  # 理论不可达（枚举契约）
        raise PeriodCompareError(f"不支持的比较模式：{mode}")

    expected = (cur_end - cur_start).days + 1
    observed = _observed_days(dates_norm, cur_start, cur_end)
    if not naturally_ended:
        completeness = "partial"
    elif observed < expected:
        completeness = "unknown"
    else:
        completeness = "complete"

    return PeriodSpec(
        mode=mode, granularity=gran,
        current_start=cur_start.date().isoformat(),
        current_end=cur_end.date().isoformat(),
        compare_start=cmp_start.date().isoformat(),
        compare_end=cmp_end.date().isoformat(),
        data_as_of=as_of.date().isoformat(),
        completeness=completeness,
        observed_days=observed, expected_days=expected,
    )


# ----------------------------------------------------------------- 聚合

def _window_aggregate(
    df: pd.DataFrame, date_column: str, metric: str | None,
    func: str, start: pd.Timestamp, end: pd.Timestamp,
) -> float | None:
    dates = pd.to_datetime(df[date_column], errors="coerce")
    mask = (dates >= start) & (dates <= end)
    sub = df.loc[mask]
    if sub.empty:
        return None
    if func == "count" and not metric:
        return float(len(sub))
    s = pd.to_numeric(sub[metric], errors="coerce")
    valid = s.dropna()
    if valid.empty:
        return None
    if func in ("sum",):
        return float(valid.sum())
    if func == "mean":
        return float(valid.mean())
    if func == "median":
        return float(valid.median())
    if func == "count_distinct":
        return float(valid.nunique())
    if func == "min":
        return float(valid.min())
    if func == "max":
        return float(valid.max())
    if func == "last":
        # 期末快照：窗口内最后一个有效观测（同粒度快照序列的末点）
        order = valid.index
        return float(valid.loc[order[-1]])
    raise PeriodCompareError(f"同环比暂不支持聚合口径：{func}")


# ----------------------------------------------------------------- 主编排

def period_comparison(
    df: pd.DataFrame,
    date_column: str,
    metric: str | None,
    func: str,
    mode: ComparisonMode | str,
    *,
    is_rate: bool = False,
) -> dict[str, Any] | None:
    """完整同环比摘要；当前期无任何有效数据时返回 None（不造数）。"""
    if date_column not in df.columns:
        raise PeriodCompareError(f"字段「{date_column}」在当前数据中不存在。")
    dates = pd.to_datetime(df[date_column], errors="coerce").dropna()
    if dates.empty:
        return None
    as_of = dates.max()
    spec = build_period_spec(as_of, str(mode), dates)

    cur = _window_aggregate(
        df, date_column, metric, func,
        pd.Timestamp(spec.current_start), pd.Timestamp(spec.current_end),
    )
    if cur is None:
        return None
    prev = _window_aggregate(
        df, date_column, metric, func,
        pd.Timestamp(spec.compare_start), pd.Timestamp(spec.compare_end),
    )
    if prev is None:
        raise PeriodCompareError(
            f"缺少对比期数据（{spec.compare_start} ~ {spec.compare_end}），"
            "无法计算同/环比。"
        )

    change = compare_change(cur, prev, is_rate=is_rate)
    label = {"mom": "月环比", "wow": "周环比", "yoy": "月同比"}[str(mode)]
    return {
        **change,
        "comparison": label,
        "current_label": (f"{spec.current_start} ~ {spec.current_end}"
                          + ("（截至日同期）" if spec.completeness != "complete" else "")),
        "compare_label": f"{spec.compare_start} ~ {spec.compare_end}",
        "completeness": spec.completeness,
        "period_spec": spec.model_dump(mode="json"),
        "metric": metric, "func": func,
    }
