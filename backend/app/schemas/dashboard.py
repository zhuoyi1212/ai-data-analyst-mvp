"""DashboardArtifact 契约（重构 Phase 2）。

由 Bundle + 真实 View Results 确定性合成（dashboard_synthesizer）；
Phase 2 只落盘/经 API 返回，Phase 3 前端才消费。全部模型 extra=forbid。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import ChartType

# ----------------------------------------------------------------- 图表规格


class ChartSpec(BaseModel):
    """前端图表契约（Phase 2 定义、Phase 3 消费）：由 view.type+params 确定性生成。"""

    model_config = ConfigDict(extra="forbid")

    type: ChartType
    title: str
    x_field: str | None = None        # 线图日期轴 / 柱图维度轴
    y_fields: list[str] = Field(default_factory=list)
    dimension: str | None = None      # 维度图的分组字段（交互联动用）
    metric: str | None = None         # 主度量字段
    interactive: bool = False         # 维度图置 True（Phase 3 全局筛选/下钻联动）


# ----------------------------------------------------------------- KPI

ChangeType = Literal["yoy", "mom", "wow", "previous_period"]


class KPI(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    value: float | None = None        # 比率分母为 0 时允许 null，不造数
    change: float | None = None       # 末两期确定性差值（小数，如 -0.123 表示 -12.3%）
    change_type: ChangeType | None = None
    unit: str = ""                    # 展示单位："¥" / "%" / ""


# ----------------------------------------------------------------- 布局

class DashboardSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: Literal["overview", "trend", "structure", "diagnosis", "detail"]
    title: str
    view_ids: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------- 筛选/下钻

class FilterDefinition(BaseModel):
    """全局筛选器推荐：低中基数主维度 + 快照实际成员取值。"""

    model_config = ConfigDict(extra="forbid")

    column: str
    label: str
    members: list[str] = Field(default_factory=list)


class DrilldownFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str
    operator: Literal["eq"] = "eq"
    value: str | int | float


class DrillChild(BaseModel):
    """下钻子成员及其指标值（如 Furniture → Tables，Profit=-42,536）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    value: float


class DrilldownSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: list[DrilldownFilter] = Field(default_factory=list)
    child_dimension: str
    top_negative_children: list[DrillChild] = Field(default_factory=list)


# ----------------------------------------------------------------- Findings

FindingType = Literal[
    "growth", "decline", "anomaly", "risk",
    "opportunity", "structure", "relationship",
]
Importance = Literal["high", "medium", "low"]


class Finding(BaseModel):
    """跨视图洞察。summary 必须含四要素：现象/位置/量化影响/关注建议。"""

    model_config = ConfigDict(extra="forbid")

    finding_id: str
    title: str
    summary: str
    type: FindingType
    evidence_view_ids: list[str] = Field(default_factory=list)
    importance: Importance
    drilldown: DrilldownSuggestion | None = None


# ----------------------------------------------------------------- Artifact

class DashboardArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle_id: str
    title: str
    kpis: list[KPI] = Field(default_factory=list)
    sections: list[DashboardSection] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    risks: list[Finding] = Field(default_factory=list)  # findings 中 type=="risk" 子集
    global_filters: list[FilterDefinition] = Field(default_factory=list)
