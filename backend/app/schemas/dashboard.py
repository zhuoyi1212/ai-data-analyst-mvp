"""DashboardArtifact 契约（P1 T06：浏览器只靠 API 即可绘图）。

由 Bundle + 真实 View Results 确定性合成（dashboard_synthesizer）。
T06 起 artifact 内嵌「视图字典」：每个 View 携带 ChartSpec、输出数据 schema、
可直接绘图的数据行（或分页/采样元数据 + data_ref）、口径与校验状态；
浏览器不需要、也无法接触服务器 parquet。全部模型 extra=forbid。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.bundle import (
    ViewStatus,
    ViewType,
    ViewValidationItem,
    ValidationLevel,
)
from app.schemas.common import ChartType
from app.schemas.insight_candidate import InsightCandidate

# ----------------------------------------------------------------- 图表规格


class ChartSpec(BaseModel):
    """前端图表契约：x/y 字段名必须真实存在于该 View 的输出 schema。

    metric 是业务指标名（如「销售额」原名 Sales），绘图一律使用 x_field/y_fields
    对应的规范化输出列（通常是 value/share），两者分离，禁止混用。
    """

    model_config = ConfigDict(extra="forbid")

    type: ChartType
    title: str
    x_field: str | None = None        # 线图日期轴 / 柱图维度轴
    y_fields: list[str] = Field(default_factory=list)
    dimension: str | None = None      # 维度图的分组字段（交互联动用）
    metric: str | None = None         # 主度量业务名（展示用，不作为绘图字段）
    interactive: bool = False         # 维度图置 True（全局筛选/下钻联动）


# ----------------------------------------------------------------- KPI

ChangeType = Literal["yoy", "mom", "wow", "previous_period"]


class KPI(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    value: float | None = None        # 比率分母为 0/无数据时为 null，绝不伪造 0
    change: float | None = None       # 正基数才给增长率（小数，如 -0.123 = -12.3%）
    change_type: ChangeType | None = None
    change_delta: float | None = None  # 负基数/跨零时给绝对差额
    change_status: str = ""            # 扭亏为盈/由盈转亏/亏损收窄/亏损扩大/不可计算
    change_hint: str = ""              # 残缺期等口径提示（如「MTD 等长同期比较」）
    unit: str = ""                     # 展示单位："¥" / "%" / ""


# ----------------------------------------------------------------- 布局

class DashboardSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: Literal["overview", "trend", "structure", "diagnosis", "detail"]
    title: str
    view_ids: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------- 视图数据契约

ColumnDType = Literal["number", "integer", "string", "datetime", "boolean"]


class ColumnSchema(BaseModel):
    """输出数据列 schema：name 是唯一允许的绘图字段引用；label 为业务展示名。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    dtype: ColumnDType
    label: str = ""


ViewDataKind = Literal["aggregate", "detail", "scatter"]


class DataPage(BaseModel):
    """明细分页：内嵌行只是当前页，其余页经 data_ref 拉取，禁止一次吐全量明细。"""

    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total_rows: int = Field(ge=0)
    total_pages: int = Field(ge=0)


class DataSample(BaseModel):
    """散点采样元数据：图形只画采样点，但统计量（相关系数等）必须基于全量有效样本。"""

    model_config = ConfigDict(extra="forbid")

    total_count: int = Field(ge=0)
    display_count: int = Field(ge=0)
    sample_method: Literal["full", "even_stride"]
    note: str = ""


class ViewDataEnvelope(BaseModel):
    """一个 View 的自足数据包：schema + 可绘图行 + 分页/采样说明 + 稳定引用。"""

    model_config = ConfigDict(extra="forbid")

    kind: ViewDataKind
    columns: list[ColumnSchema] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    page: DataPage | None = None
    sample: DataSample | None = None
    # 稳定引用（运行内），如 runs/{run_id}/views/view_03/rows；明细翻页走它
    data_ref: str = ""


DashboardValidity = ValidationLevel | Literal["unknown"]


class ViewCard(BaseModel):
    """视图字典条目：图表契约 + 数据 + 口径 + 执行/校验状态，一卡自足。"""

    model_config = ConfigDict(extra="forbid")

    view_id: str
    title: str
    question: str
    type: ViewType
    section_id: str | None = None
    chart_spec: ChartSpec | None = None
    data: ViewDataEnvelope | None = None
    interpretation: str = ""          # 一句话读图指引（中文，数字来自该视图）
    status: ViewStatus
    validity: str = "unknown"          # pass/warn/fail/no_data；status≠success 时 unknown
    consumable: bool = False
    reason: str = ""                   # failed/no_data 等中文原因
    checks: list[ViewValidationItem] = Field(default_factory=list)
    metric_label: str = ""             # 业务指标名（与绘图字段分离）

    # T07：默认可见性与价值评估
    role: Literal["presentation", "computation"] = "presentation"
    default_hidden: bool = False       # computation 证据不足时默认隐藏（结果仍保留）
    hide_reasons: list[str] = Field(default_factory=list)
    value_scores: dict[str, float] = Field(default_factory=dict)

    # Task 7：深挖链探针卡（id 与普通 view 同字典，但 ref_type=probe）
    ref_type: Literal["view", "probe"] = "view"


# ----------------------------------------------------------------- 筛选/范围

class FilterDefinition(BaseModel):
    """全局筛选器推荐：低中基数主维度 + 快照实际成员取值。"""

    model_config = ConfigDict(extra="forbid")

    column: str
    label: str
    members: list[str] = Field(default_factory=list)
    selected_values: list[str] | None = None  # None=全部成员；列表=当前范围已选


class AppliedFilter(BaseModel):
    """已生效的全局筛选（随运行版本固化）；values 为空表示该维度不筛选。"""

    model_config = ConfigDict(extra="forbid")

    column: str = Field(min_length=1)
    values: list[str] = Field(default_factory=list)


class DashboardScope(BaseModel):
    """整批结果共享的分析范围：KPI/图表/Finding 必须全部在同一范围内计算。"""

    model_config = ConfigDict(extra="forbid")

    filters: list[AppliedFilter] = Field(default_factory=list)
    snapshot_rows: int = 0             # 全量快照行数
    participating_rows: int = 0        # 应用全局筛选后的行数


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


class FailedView(BaseModel):
    """部分失败摘要（state=partial 时告诉用户哪些视角不可用及原因）。"""

    model_config = ConfigDict(extra="forbid")

    view_id: str
    title: str
    reason: str


class RefineRequest(BaseModel):
    """全局筛选重算请求：filters 为空列表 = 回到全量快照范围。"""

    model_config = ConfigDict(extra="forbid")

    filters: list[AppliedFilter] = Field(default_factory=list)


# ----------------------------------------------------------------- Artifact

# ready=至少有一个可消费视图且无失败；partial=有可消费视图但存在失败/不可消费；
# empty=筛选后所有视图均无数据（空态，不是错误，绝不伪造 0）
DashboardState = Literal["ready", "partial", "empty"]


class DashboardArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = ""      # P0 T01：发布该仪表盘的运行版本
    bundle_id: str
    title: str
    state: DashboardState = "empty"
    scope: DashboardScope = Field(default_factory=DashboardScope)
    kpis: list[KPI] = Field(default_factory=list)
    sections: list[DashboardSection] = Field(default_factory=list)
    views: dict[str, ViewCard] = Field(default_factory=dict)  # T06：视图字典
    insights: list[InsightCandidate] = Field(default_factory=list)  # Insight-first：业务事实（评分排序）
    findings: list[Finding] = Field(default_factory=list)
    risks: list[Finding] = Field(default_factory=list)  # findings 中 type=="risk" 子集
    failed_views: list[FailedView] = Field(default_factory=list)
    global_filters: list[FilterDefinition] = Field(default_factory=list)
    layout: "DashboardLayout | None" = None  # Task 7：12-column 组合布局


# ------------------------------------------------------- Task 7 布局契约

LayoutRole = Literal[
    "kpi", "hero", "primary", "supporting", "diagnostic", "findings"
]


class DashboardLayoutItem(BaseModel):
    """12-column 网格中的一个排版项。

    ref_type=meta 时 item_id 为面板伪 id（KPI 条 / Findings）；
    view/probe 时 item_id 等于 views 字典中的 view_id（探针同字典）。
    前端按连续顺序贪心换行即可：每行 col_span 之和 ≤12。
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str
    role: LayoutRole
    ref_type: Literal["view", "probe", "meta"] = "view"
    col_span: int = Field(ge=1, le=12)
    row_span: int = Field(ge=1, le=4)
    order: int = Field(ge=0)
    rationale: str = Field(min_length=1)
    default_hidden: bool = False


class DashboardLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[DashboardLayoutItem] = Field(default_factory=list)
    generated_at: str


DashboardArtifact.model_rebuild()
