"""数据字典契约（Semantic Profiler 产物）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Confidence, FieldInfo, SemanticType

# 指标默认聚合口径（P0 T03 MetricSpec）
MetricAggregation = Literal[
    "sum",             # 可加指标：金额/销量等
    "mean",            # 率/评分等不可加指标：均值仅作展示口径
    "median",
    "count",
    "count_distinct",  # 订单数等：依赖业务键去重，不能拿行数冒充
    "snapshot_last",   # 库存等：每实体期末快照再汇总
    "none",            # 明确不支持自动聚合（关键歧义待澄清）
]
MetricDirection = Literal["higher_better", "lower_better", "neutral"]
MetricTimeRole = Literal["event", "snapshot", "none"]
MetricNullPolicy = Literal["exclude", "keep_as_zero", "require_confirmation"]


class MetricSpec(BaseModel):
    """最小指标口径规格（T03）：所有聚合/份额/同环比都必须遵从该口径。

    字段名只能产出候选口径；关键歧义（如库存 vs 销量、订单数 vs 行数）
    需要单点用户澄清，未确认前禁止静默求和。
    """

    model_config = ConfigDict(extra="forbid")

    aggregation: MetricAggregation
    additive: bool                     # 是否可跨实体/跨组相加
    grain: str = "row"                 # 指标天然粒度（行/订单/实体期末快照…）
    entity_key: str | None = None      # count_distinct / snapshot_last 依赖的业务键
    unit: str | None = None
    direction: MetricDirection = "neutral"
    time_role: MetricTimeRole = "event"
    allowed_negative: bool = False     # 利润等业务合法负值必须为 True
    null_policy: MetricNullPolicy = "exclude"
    weighted: bool = False             # 比率是否已按分母加权（无分母记录时 False）
    denominator: str | None = None     # 比率/率类指标的分母字段（已知时）
    note: str = ""                     # 口径限制的人读说明（如「未按分母加权」）


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
    # P0 T03：指标口径规格（旧字典缺省为 None，由规则推断兜底，不阻断加载）
    metric_spec: MetricSpec | None = None
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
