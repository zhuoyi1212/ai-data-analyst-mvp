"""分析方案契约：10 个有限算子的判别联合（附录 A）。

任何算子以外的步骤都无法通过本模型校验；执行器只执行这里定义过的算子。
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import (
    AggFunc,
    ChartType,
    ComparePeriod,
    CorrMethod,
    FilterOperator,
    OutlierMethod,
    SortOrder,
    TimeGranularity,
)

_STRICT = ConfigDict(extra="forbid")


class _StepBase(BaseModel):
    model_config = _STRICT
    step_id: str = Field(min_length=1)
    description: str = ""  # 人读公式描述
    depends_on: list[str] = Field(default_factory=list)  # 默认顺序执行


class FilterParams(BaseModel):
    model_config = _STRICT
    column: str = Field(min_length=1)
    operator: FilterOperator
    value: Any = None


class FilterStep(_StepBase):
    op: Literal["filter"]
    params: FilterParams


class AggregateParams(BaseModel):
    model_config = _STRICT
    column: str | None = None  # func=count 时可为空（计数行）
    func: AggFunc


class AggregateStep(_StepBase):
    op: Literal["aggregate"]
    params: AggregateParams


class GroupByParams(BaseModel):
    model_config = _STRICT
    dimension: str = Field(min_length=1)
    metric: str | None = None
    func: AggFunc = AggFunc.sum
    order: SortOrder = SortOrder.desc
    limit: int | None = Field(default=None, ge=1, le=500)


class GroupByStep(_StepBase):
    op: Literal["group_by"]
    params: GroupByParams


class ShareParams(BaseModel):
    model_config = _STRICT
    dimension: str = Field(min_length=1)
    metric: str | None = None
    func: AggFunc = AggFunc.sum


class ShareStep(_StepBase):
    op: Literal["share"]
    params: ShareParams


class TopNParams(BaseModel):
    model_config = _STRICT
    dimension: str = Field(min_length=1)
    metric: str | None = None
    func: AggFunc = AggFunc.sum
    n: int = Field(ge=1, le=50)
    order: SortOrder = SortOrder.desc


class TopNStep(_StepBase):
    op: Literal["top_n"]
    params: TopNParams


class TimeSeriesParams(BaseModel):
    model_config = _STRICT
    date_column: str = Field(min_length=1)
    granularity: TimeGranularity
    metric: str | None = None
    func: AggFunc = AggFunc.sum


class TimeSeriesStep(_StepBase):
    op: Literal["time_series"]
    params: TimeSeriesParams


class PeriodCompareParams(BaseModel):
    model_config = _STRICT
    date_column: str = Field(min_length=1)
    period: ComparePeriod
    metric: str | None = None
    func: AggFunc = AggFunc.sum


class PeriodCompareStep(_StepBase):
    op: Literal["period_compare"]
    params: PeriodCompareParams


class CompareGroupsParams(BaseModel):
    model_config = _STRICT
    dimension: str = Field(min_length=1)
    members: list[str] = Field(min_length=2)
    metric: str | None = None
    func: AggFunc = AggFunc.sum


class CompareGroupsStep(_StepBase):
    op: Literal["compare_groups"]
    params: CompareGroupsParams


class CorrelationParams(BaseModel):
    model_config = _STRICT
    column_x: str = Field(min_length=1)
    column_y: str = Field(min_length=1)
    method: CorrMethod = CorrMethod.pearson


class CorrelationStep(_StepBase):
    op: Literal["correlation"]
    params: CorrelationParams


class OutlierFlagParams(BaseModel):
    model_config = _STRICT
    column: str = Field(min_length=1)
    method: OutlierMethod = OutlierMethod.iqr
    threshold: float = Field(gt=0, le=10)  # IQR 倍数或 z 分数阈值

    @field_validator("threshold")
    @classmethod
    def _default_range(cls, v: float) -> float:
        return v


class OutlierFlagStep(_StepBase):
    op: Literal["outlier_flag"]
    params: OutlierFlagParams
    date_column: str | None = None  # 可选：按时间序列展示离群


PlanStep = Annotated[
    Union[
        FilterStep,
        AggregateStep,
        GroupByStep,
        ShareStep,
        TopNStep,
        TimeSeriesStep,
        PeriodCompareStep,
        CompareGroupsStep,
        CorrelationStep,
        OutlierFlagStep,
    ],
    Field(discriminator="op"),
]


class AnalysisPlan(BaseModel):
    model_config = _STRICT
    plan_id: str | None = None
    question: str = Field(min_length=1)
    data_scope: str = ""
    steps: list[PlanStep] = Field(min_length=1)
    expected_shape: str = ""
    chart_hint: ChartType | None = None


class PlanArtifact(BaseModel):
    """方案阶段落盘产物：含服务端权威图表类型、确认状态与方案哈希。"""

    model_config = _STRICT
    question: str = Field(min_length=1)
    plan: AnalysisPlan
    chart: ChartType
    locked: bool = False
    plan_hash: str | None = None
    source_question_index: int | None = None
