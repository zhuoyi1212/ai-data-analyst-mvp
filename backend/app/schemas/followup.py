"""Follow-up 追问契约（FR-11）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FollowUpQuestion(BaseModel):
    text: str = Field(min_length=4)
    rationale: str = Field(min_length=10)  # 推荐依据，必须接地
    fields: list[str] = Field(min_length=1)
    target_op: str  # 必须在算子目录内
    plan_hint: dict[str, Any] = Field(default_factory=dict)  # 预填参数（粒度/维度等）


class FollowUpSet(BaseModel):
    questions: list[FollowUpQuestion] = Field(min_length=3, max_length=5)
