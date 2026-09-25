"""Insight 结构化契约（FR-9 / FR-10）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import Confidence, InsightType

RefKind = Literal["metric", "row", "summary"]


class GroundedNumber(BaseModel):
    """文本中出现的每个数字：数值 + 指向引擎结果的引用。"""

    value: float
    label: str = ""
    ref_step: str = Field(min_length=1)
    ref_kind: RefKind
    ref_keys: dict[str, Any] = Field(default_factory=dict)


class Insight(BaseModel):
    text: str = Field(min_length=10)
    type: InsightType
    evidence_refs: list[str] = Field(min_length=1)  # 步骤 ID 列表
    numbers: list[GroundedNumber] = Field(min_length=1)
    confidence: Confidence = Confidence.medium  # 服务端会按可信度策略覆盖
    confidence_reason: str = ""
    needs_further_validation: bool = False
    disclaimer: str | None = None


class InsightSet(BaseModel):
    insights: list[Insight] = Field(min_length=2, max_length=5)
