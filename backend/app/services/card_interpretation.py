"""图表解读文案（纯确定性，无 LLM）。

为每张视图卡片生成一句话「读图指引」：这张图在看什么、关键高低点在哪、
读者应当如何使用它。文案中的数字全部直接取自该视图的结果数据。
"""
from __future__ import annotations

import pandas as pd

from app.services.field_labels import field_label


def _label_of(name: str) -> str:
    return field_label(name)


def _num(v) -> str:
    """结果数字的展示格式：万以上千分位取整，其余保留原值（避免科学计数法）。"""
    v = float(v)
    if abs(v) >= 10000:
        return f"{v:,.0f}"
    return f"{v:g}"


def _time_label(v) -> str:
    """时间轴标签：月初时间戳只显示到月，避免冗长的 00:00:00。"""
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m") if v.day == 1 else v.strftime("%Y-%m-%d")
    return str(v)


def _extreme(df: pd.DataFrame, key: str, value: str) -> tuple:
    ordered = df.sort_values(value, ascending=False)
    top, bottom = ordered.iloc[0], ordered.iloc[-1]
    return (
        top[key], round(float(top[value]), 2),
        bottom[key], round(float(bottom[value]), 2),
    )


def interpret_view(
    *,
    view_type: str,
    df: pd.DataFrame,
    metric_fields: list[str] | None = None,
    dimension_fields: list[str] | None = None,
    title_hint: str = "",
) -> str:
    """按视图类型与真实结果生成一句话解读。空/缺列时安全降级。"""
    metric_fields = metric_fields or []
    dimension_fields = dimension_fields or []
    if df is None or df.empty:
        return ""

    metric = _label_of(metric_fields[0]) if metric_fields else "指标"

    # ---- 时间序列优先：即使 bundle 把时间列放进了 dimension_fields ----
    if view_type == "trend" and "value" in df.columns:
        time_cols = [
            c for c in dimension_fields if c in df.columns
        ] or [c for c in df.columns if c != "value"]
        if time_cols:
            t = time_cols[0]
            top_t, top_v, bottom_t, bottom_v = _extreme(df, t, "value")
            return (
                f"{metric}随{_label_of(t)}的变化趋势：{_time_label(top_t)} "
                f"达到峰值（{_num(top_v)}），{_time_label(bottom_t)} "
                f"为谷值（{_num(bottom_v)}），用于判断上升/下滑走势与拐点。"
            )

    # ---- 有维度 + value：对比/排名/拆解类 ----
    dim_cols = [c for c in dimension_fields if c in df.columns]
    if "value" in df.columns and dim_cols:
        dim, dim_name = dim_cols[0], _label_of(dim_cols[0])
        top_row, top_v, bottom_row, bottom_v = _extreme(df, dim, "value")
        top_label, bottom_label = str(top_row), str(bottom_row)

        # 仅一个分组：不做最高/最低对比，避免零信息自比
        if top_label == bottom_label:
            return (
                f"当前范围内{metric}按{dim_name}仅含一个分组"
                f"（{top_label}：{_num(top_v)}）。"
            )

        if view_type in ("breakdown",) or "share" in df.columns:
            share = df.sort_values("share", ascending=False).iloc[0] \
                if "share" in df.columns else None
            if share is not None:
                pct = round(float(share["share"]) * 100, 1)
                return (
                    f"展示{metric}在各{dim_name}间的构成占比："
                    f"{share[dim]} 占比最大（{pct:g}%），"
                    "用于识别结构是否集中。"
                )

        if view_type in ("ranking", "top_n"):
            # 标题含 Bottom：榜单是倒序的，文案语义相应反转
            if "bottom" in title_hint.lower():
                return (
                    f"{metric}末 {len(df)} 位：{top_label} 表现最差"
                    f"（{_num(top_v)}），用于定位拖累最大的{dim_name}。"
                )
            return (
                f"{metric}排名（前 {len(df)} 位）：首位 {top_label}"
                f"（{_num(top_v)}），用于聚焦头部{dim_name}。"
            )

        if view_type in ("contribution",):
            return (
                f"各{dim_name}对{metric}总变化的贡献拆解："
                f"{top_label} 贡献最大（{_num(top_v)}），"
                f"{bottom_label}（{_num(bottom_v)}）；正负贡献相加等于总变化量。"
            )

        return (
            f"对比各{dim_name}的{metric}：{top_label} 最高（{_num(top_v)}），"
            f"{bottom_label} 最低（{_num(bottom_v)}），用于定位表现差异最大的"
            f"{dim_name}。"
        )

    # ---- 概览：无维度总量 ----
    if view_type == "overview" and "value" in df.columns:
        v = round(float(df["value"].iloc[0]), 2)
        return f"{metric}全局总量为 {_num(v)}，作为其他对比的基准口径。"

    # ---- 相关散点 ----
    if view_type == "relationship":
        nums = [
            c for c in df.columns
            if pd.api.types.is_numeric_dtype(df[c])
        ]
        if len(nums) >= 2:
            return (
                f"{_label_of(nums[0])} 与 {_label_of(nums[1])} 的相关分布"
                "（每个点为一个样本/分组）：点的走向提示同向或反向关联，"
                "相关不等于因果。"
            )

    # ---- 离群明细 ----
    if view_type == "anomaly":
        n = int(df["is_outlier"].sum()) if "is_outlier" in df.columns else len(df)
        return (
            f"被离群规则标记的明细记录（共 {n} 条，按异常程度排序），"
            "用于核查极端或异常数据。"
        )

    # ---- 率结构分解 ----
    if view_type == "rate_shift":
        return (
            f"把{metric}的变化分解为组内变化（within）与结构变化（mix），"
            "用于区分『各组自身变化』与『占比结构变化』。"
        )

    return ""
