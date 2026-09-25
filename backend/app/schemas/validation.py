"""Validate 阶段契约：五项确定性检查结果（FR-7）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import CheckLevel

Overall = Literal["pass", "warn", "fail"]


class CheckItem(BaseModel):
    code: Literal["coverage", "cross_check", "null_handling", "shape", "plan_consistency"]
    title: str
    level: CheckLevel
    detail: str
    numbers: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    session_id: str
    overall: Overall
    items: list[CheckItem]
    acknowledged: bool = False
    validated_at: str
