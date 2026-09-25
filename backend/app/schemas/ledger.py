"""计算台账契约（FR-6）：每次执行全留痕、可检视。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ChartType


class StepRecord(BaseModel):
    step_id: str
    op: str
    description: str = ""
    params: dict[str, Any]
    input_rows: int
    output_rows: int
    output_columns: list[str]
    formula: str
    summary: dict[str, Any] = Field(default_factory=dict)


class Ledger(BaseModel):
    session_id: str
    question: str
    data_scope: str = ""
    plan_hash: str
    snapshot_hash: str
    snapshot_rows: int
    participating_rows: int  # 进入末端计算的行数（筛选后）
    chart: ChartType
    steps: list[StepRecord]
    result_columns: list[str]
    result_rows_total: int
    result_preview: list[dict[str, Any]]  # 最多展示 1,000 行；全量见 result.parquet
    elapsed_ms: float
    executed_at: str
