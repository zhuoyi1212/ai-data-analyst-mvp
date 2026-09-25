"""数据字典契约（Semantic Profiler 产物）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import Confidence, FieldInfo, SemanticType


class FieldProfile(BaseModel):
    name: str
    physical_type: str  # integer / float / string / boolean / datetime
    semantic_type: SemanticType
    meaning: str = ""  # 业务含义解释
    unit: str | None = None
    candidates: list[str] = Field(default_factory=list)  # 候选业务含义
    confidence: Confidence = Confidence.low
    examples: list[Any] = Field(default_factory=list)
    is_date: bool = False
    is_metric: bool = False
    null_rate: float = 0.0
    cardinality: int = 0
    stats: dict[str, Any] = Field(default_factory=dict)
    rule_hints: list[str] = Field(default_factory=list)
    # 确认状态
    confirmed: bool = False  # 是否无需用户确认（high 且语义明确）
    confirmed_by_user: bool = False
    ignored: bool = False


class FieldRelation(BaseModel):
    type: Literal["product", "correlation"]
    columns: list[str]
    note: str
    coefficient: float | None = None


class DataDictionary(BaseModel):
    session_id: str
    fields: list[FieldProfile]
    relations: list[FieldRelation] = Field(default_factory=list)
    complete: bool = False  # 语义确认闸门是否通过

    def field(self, name: str) -> FieldProfile | None:
        return next((f for f in self.fields if f.name == name), None)

    def usable_field_infos(self) -> list[FieldInfo]:
        """供方案/算子使用：忽略字段被排除。"""
        return [
            FieldInfo(name=f.name, semantic_type=f.semantic_type, ignored=f.ignored)
            for f in self.fields
            if not f.ignored
        ]

    def date_fields(self) -> list[str]:
        return [f.name for f in self.fields if not f.ignored and f.semantic_type == SemanticType.date]


class LLMFieldEnrichment(BaseModel):
    """LLM 语义补全的单字段输出契约。"""

    name: str
    semantic_type: SemanticType
    meaning: str = Field(min_length=1)
    unit: str | None = None
    candidates: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.medium


class LLMProfileResult(BaseModel):
    fields: list[LLMFieldEnrichment] = Field(min_length=1)
