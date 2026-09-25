"""智能问题推荐契约（FR-4）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import Confidence, QuestionCategory


class RecommendedQuestion(BaseModel):
    text: str = Field(min_length=4)
    category: QuestionCategory
    rationale: str = Field(min_length=10)  # 推荐依据：必须具体
    fields: list[str] = Field(min_length=1)
    target_op: str  # 必须在算子目录内
    confidence: Confidence = Confidence.medium
    plan_hint: dict[str, Any] = Field(default_factory=dict)  # 离线规则降级时预填方案参数


class QuestionSet(BaseModel):
    questions: list[RecommendedQuestion] = Field(min_length=1)
