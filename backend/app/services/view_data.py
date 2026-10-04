"""View 数据信封构建（P1 T06：浏览器只靠 API 即可绘图）。

把 result.parquet 转成浏览器自足的 ViewDataEnvelope：
- aggregate：聚合结果行数很小（受维度基数约束），全量内嵌；
- scatter：相关散点默认最多画 SCATTER_CAP 个点（even_stride 等距采样），
  但相关系数等统计量在 View 执行时已基于全量有效样本计算，采样只影响展示；
- detail：离群明细只内嵌第一页，其余页经 data_ref 翻页拉取，禁止一次吐全量。

浏览器不需要、也无法接触服务器 parquet。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from app.schemas.dashboard import (
    ColumnSchema,
    DataPage,
    DataSample,
    ViewDataEnvelope,
)
from app.services.executor import clean_rows

SCATTER_CAP = 500
DETAIL_PAGE_SIZE = 50


# ----------------------------------------------------------------- schema

def _column_dtype(s: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(s):
        return "boolean"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "datetime"
    if pd.api.types.is_integer_dtype(s):
        return "integer"
    if pd.api.types.is_numeric_dtype(s):
        return "number"
    return "string"


def build_columns(
    df: pd.DataFrame, labels: dict[str, str] | None = None
) -> list[ColumnSchema]:
    """输出数据列 schema：name 是唯一允许的绘图字段引用；label 为中文展示名。"""
    labels = labels or {}
    return [
        ColumnSchema(
            name=str(col), dtype=_column_dtype(df[col]),
            label=labels.get(str(col), str(col)),
        )
        for col in df.columns
    ]


# ----------------------------------------------------------------- 排序/分页

def ordered_detail(df: pd.DataFrame) -> pd.DataFrame:
    """detail 固定顺序：被标记的离群行优先（稳定排序），翻页口径一致。"""
    if "is_outlier" in df.columns:
        return df.sort_values("is_outlier", ascending=False, kind="stable")
    return df


def total_pages(total_rows: int, page_size: int) -> int:
    return math.ceil(total_rows / page_size) if page_size else 0


def slice_page(
    df: pd.DataFrame, *, page: int, page_size: int
) -> tuple[list[dict], DataPage]:
    """对（已排序的）df 切页，返回内嵌行 + 分页元数据。"""
    total_rows = len(df.index)
    pages = total_pages(total_rows, page_size)
    page = max(1, min(page, pages)) if pages else 1
    start = (page - 1) * page_size
    part = df.iloc[start:start + page_size]
    meta = DataPage(
        page=page, page_size=page_size,
        total_rows=total_rows, total_pages=pages,
    )
    return clean_rows(part, page_size), meta


# ----------------------------------------------------------------- 采样

def scatter_indices(total: int, cap: int = SCATTER_CAP) -> np.ndarray:
    """等距（even_stride）采样下标：首尾必取、均匀分布；不足上限时全取。"""
    if total <= cap:
        return np.arange(total)
    return np.unique(np.linspace(0, total - 1, cap, dtype=int))


# ----------------------------------------------------------------- 信封

def build_envelope(
    view_type: str,
    df: pd.DataFrame,
    data_ref: str,
    column_labels: dict[str, str] | None = None,
) -> ViewDataEnvelope:
    """按 View 类型构建自足数据信封。data_ref 为运行内稳定 API 路径。"""
    columns = build_columns(df, column_labels)

    if view_type == "relationship":
        total = len(df.index)
        idx = scatter_indices(total)
        sample_df = df.iloc[idx]
        sampled = total > SCATTER_CAP
        return ViewDataEnvelope(
            kind="scatter",
            columns=columns,
            rows=clean_rows(sample_df, SCATTER_CAP),
            sample=DataSample(
                total_count=total,
                display_count=len(idx),
                sample_method="even_stride" if sampled else "full",
                note=(
                    f"图形展示 {len(idx)} 个等距采样点；"
                    "相关系数基于全部有效成对样本计算。"
                    if sampled else "全部有效成对样本。"
                ),
            ),
            data_ref=data_ref,
        )

    if view_type == "anomaly":
        ordered = ordered_detail(df)
        rows, page_meta = slice_page(
            ordered, page=1, page_size=DETAIL_PAGE_SIZE
        )
        return ViewDataEnvelope(
            kind="detail",
            columns=columns,
            rows=rows,
            page=page_meta,
            data_ref=data_ref,
        )

    return ViewDataEnvelope(
        kind="aggregate",
        columns=columns,
        rows=clean_rows(df, len(df.index)),
        data_ref=data_ref,
    )
