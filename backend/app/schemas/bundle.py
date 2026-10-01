"""AnalysisBundle 契约：一次自动多维分析的顶层对象（重构 Phase 1）。

层级关系：
    AnalysisBundle（一份数据的一次自动分析）
      └─ AnalysisView（一个独立分析视角 = question + AnalysisPlan）
           └─ ViewExecutionResult（该视角执行后的状态/台账/结果摘要）

设计要点：
- AnalysisView 内嵌的仍是既有 schemas.plan.AnalysisPlan（Phase 2 起 11 算子白名单），
  Bundle 层只负责「一次规划并执行多个 Plan」，不改变算子契约；
- chart 类型 Phase 1 复用 ChartType 枚举；完整 ChartSpec 在 Phase 2 定义；
- provenance 标记每个 view 的来源（candidate 规则），便于后续 LLM Selection 替换。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import ChartType
from app.schemas.ledger import StepRecord
from app.schemas.plan import AnalysisPlan

_STRICT = ConfigDict(extra="forbid")

# T07：区分内部计算任务与展示 View——
# presentation：默认展示的视图（Seed：核心 KPI / 双指标对齐趋势 / 一个基准拆分）；
# computation ：为业务问题提供证据的内部计算任务，默认不占展示位，
#               结果保留可按需返回（含阴性结果，如 r=0、无离群）。
ViewRole = Literal["presentation", "computation"]


class ViewType(str, Enum):
    overview = "overview"            # 总体表现
    trend = "trend"                  # 时间趋势
    breakdown = "breakdown"          # 结构/占比拆解
    comparison = "comparison"        # 维度对比
    ranking = "ranking"              # Top N 排名
    relationship = "relationship"    # 指标关系（相关）
    anomaly = "anomaly"              # 异常/离群
    profitability = "profitability"  # 盈利/效率（Phase 2：派生指标 ratio）
    # T08：跨指标信号（分解表，默认 computation）
    contribution = "contribution"    # 分组变化贡献（加法会计恒等式）
    rate_shift = "rate_shift"        # 率的结构变化（within/mix/interaction）


# 候选生成 → 选择 流水线中，候选与最终 view 的分析类型同一集合。
# profitability Phase 1 不产出，但类型先占位，保证契约前向兼容。

# 每个 view 类型确定性映射的图表类型（Phase 1 规则）
VIEW_CHART: dict[ViewType, ChartType] = {
    ViewType.overview: ChartType.metric,
    ViewType.trend: ChartType.line,
    ViewType.breakdown: ChartType.pie,
    ViewType.comparison: ChartType.bar,
    ViewType.ranking: ChartType.bar,
    ViewType.relationship: ChartType.scatter,
    ViewType.anomaly: ChartType.line_outlier,
    ViewType.profitability: ChartType.bar,  # 各维度成员的派生比率（利润率）
    # T08：第一版以可回算分解表呈现（chart_spec 为 None，前端表格展示）
    ViewType.contribution: ChartType.table,
    ViewType.rate_shift: ChartType.table,
}


class AnalysisView(BaseModel):
    """一个独立分析视角：业务问题 + 可执行方案（仍为 10 算子白名单）。"""

    model_config = _STRICT
    view_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    type: ViewType
    question: str = Field(min_length=1)
    plan: AnalysisPlan
    chart: ChartType
    priority: int = 0  # 数值越小越靠前（Dashboard 布局用）

    # 涉及的主指标/维度，供选择阶段去重与多样性度量，也供前端展示口径
    metric_fields: list[str] = Field(default_factory=list)
    dimension_fields: list[str] = Field(default_factory=list)

    # 候选来源标记：规则名 + 选择理由，便于追溯与后续替换为 LLM Selection
    source: str = "rule"
    selection_reason: str = ""

    # T07：展示 View 或内部计算任务（默认 presentation 保持向后兼容）
    role: ViewRole = "presentation"


class AnalysisBundle(BaseModel):
    """一份数据的一次自动多维分析蓝图。"""

    model_config = _STRICT
    bundle_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    primary_metrics: list[str] = Field(default_factory=list)
    primary_dimensions: list[str] = Field(default_factory=list)
    analysis_views: list[AnalysisView] = Field(min_length=1)


# ---------------------------------------------------------------- 执行结果

class ViewStatus(str, Enum):
    success = "success"
    failed = "failed"
    skipped = "skipped"  # 预留：前置数据条件不足时不执行（Phase 1 一般在规划期剔除）


# 有效性级别（P0 T04）：no_data 与 fail 分开——
# 引擎成功但零有效样本不是失败，却同样不可消费，禁止拿 0 充当 KPI。
ValidationLevel = Literal["pass", "warn", "fail", "no_data"]


class ViewValidationItem(BaseModel):
    """View 级轻量校验项（P0：coverage/shape/null_handling/reconciliation）。"""

    model_config = _STRICT
    code: Literal["coverage", "shape", "null_handling", "reconciliation"]
    level: ValidationLevel
    detail: str = ""
    numbers: dict[str, Any] = Field(default_factory=dict)


class ViewExecutionResult(BaseModel):
    """单个 view 的执行结果与状态（partial success 的基本单元）。

    T04 起明确区分两个维度：
    - status：引擎执行状态（success/failed/skipped）；
    - validity：结果有效性（由 checks 汇总：pass/warn/fail/no_data）。
    只有 status=success 且无 fail/no_data 才可被 Dashboard 消费。
    """

    model_config = _STRICT
    view_id: str
    status: ViewStatus
    reason: str = ""  # failed/skipped 时的中文原因

    chart: ChartType | None = None
    steps: list[StepRecord] = Field(default_factory=list)
    result_columns: list[str] = Field(default_factory=list)
    result_rows_total: int = 0
    result_preview: list[dict[str, Any]] = Field(default_factory=list)
    participating_rows: int = 0
    snapshot_rows: int = 0
    elapsed_ms: float = 0.0

    checks: list[ViewValidationItem] = Field(default_factory=list)

    @property
    def validity(self) -> str:
        """pass > warn > no_data > fail（取最差级别；无校验项视为 fail）。"""
        order = {"pass": 0, "warn": 1, "no_data": 2, "fail": 3}
        if self.status != ViewStatus.success:
            return "fail"
        if not self.checks:
            return "fail"
        return max((c.level for c in self.checks), key=lambda lv: order[lv])

    @property
    def consumable(self) -> bool:
        """KPI / Chart / Finding / probe 共享的唯一消费门禁。"""
        return self.validity in ("pass", "warn")

    @property
    def passed(self) -> bool:
        """向后兼容别名。"""
        return self.consumable


class BundleExecutionResult(BaseModel):
    """一次批量执行的汇总（允许部分失败）。"""

    model_config = _STRICT
    run_id: str = ""  # P0 T01：执行结果归属的运行版本
    bundle_id: str
    total: int
    succeeded: int
    failed: int
    views: list[ViewExecutionResult]
