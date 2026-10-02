"""Diagnostic Search 契约（T09）：预算受控的自适应维度诊断。

全部模型 extra="forbid"。数值仍只来自确定性算子；本层只定义搜索状态、
树节点、probe 与评分的结构。评分字段为**启发式排序分**，不是因果解释度，
也不是统计信息增益。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

_STRICT = ConfigDict(extra="forbid")

# 搜索状态
SearchState = Literal["running", "completed"]

# 节点状态：open（可展开）/ expanded（已展开子节点）/ stopped（按停止条件关闭）
NodeState = Literal["open", "expanded", "stopped"]

# 停止原因（停止≠失败：弱信号提前停止是正常行为）
StopReason = Literal[
    "no_signal",            # probe 评分低于展开阈值
    "insufficient_data",    # support 不足 / 零行 / 单成员分组
    "insufficient_impact",  # 影响不足
    "no_new_evidence_2x",   # 连续两次无新证据
    "branch_failed_2x",     # 同分支连续失败
    "budget_exhausted",     # probe / terminal / round 预算到顶
    "depth_exhausted",      # 深度到顶
    "root_completed",       # 正常收敛
]


class ScopeFilter(BaseModel):
    """路径上的一个维度范围约束（列 + 成员集合）。"""

    model_config = _STRICT
    column: str = Field(min_length=1)
    values: list[str] = Field(min_length=1)


class SearchBudget(BaseModel):
    """预算硬边界与已用量；used_* 只从持久化状态恢复，失败也计数。"""

    model_config = _STRICT
    max_rounds: int = 4
    max_probes: int = 8
    max_depth: int = 3
    max_plans_per_probe: int = 3
    max_terminal_executions: int = 24

    used_rounds: int = 0
    used_probes: int = 0
    used_terminal_executions: int = 0

    def rounds_left(self) -> int:
        return max(0, self.max_rounds - self.used_rounds)

    def probes_left(self) -> int:
        return max(0, self.max_probes - self.used_probes)

    def executions_left(self) -> int:
        return max(0, self.max_terminal_executions - self.used_terminal_executions)


class ProbeScores(BaseModel):
    """启发式评分（0–1，cardinality_penalty 为惩罚项）。

    明确：这是基于数据分布的启发式排序，不代表因果解释度或统计信息增益。
    """

    model_config = _STRICT
    concentration: float       # 分组集中度（HHI / top-1 占比）
    impact: float              # 组间差 / 变化贡献（标准化）
    support: float             # 最小组行数 / 独立实体数
    stability: float           # 基期→当前结构权重变化
    novelty: float             # 对已探索分支/现有视图的新颖性
    cardinality_penalty: float # 高基数惩罚
    total: float               # 加权组合后用于排序/展开判定


class PlanBrief(BaseModel):
    """probe 内单个终端计划的摘要与执行结果。"""

    model_config = _STRICT
    op: str
    params: dict[str, Any]
    status: Literal["executed", "failed", "skipped"]
    reason: str = ""


class ProbeRecord(BaseModel):
    """一次维度试探（一个维度在一个 scope 下 = 1 probe）。"""

    model_config = _STRICT
    probe_id: str
    node_id: str
    round: int
    dimension: str
    scope_filters: list[ScopeFilter]
    plans: list[PlanBrief]
    executed_count: int
    failed_count: int
    consumable: bool
    observations: dict[str, Any] = Field(default_factory=dict)
    scores: ProbeScores | None = None
    failed_reason: str = ""


class SearchNode(BaseModel):
    """诊断树上的节点：继承有序 scope 链，试探一个维度。"""

    model_config = _STRICT
    node_id: str
    parent_id: str | None = None
    depth: int = Field(ge=0)
    scope_filters: list[ScopeFilter] = Field(default_factory=list)
    dimension: str = ""
    state: NodeState = "open"
    stop_reason: StopReason | None = None
    scores: ProbeScores | None = None


class RoundRecord(BaseModel):
    """一轮的可核实摘要（不输出模型内部思维）。"""

    model_config = _STRICT
    round: int
    action_summary: str
    probe_ids: list[str] = Field(default_factory=list)
    next_action: str = ""


class DiagnosticSearch(BaseModel):
    """一次诊断搜索的完整持久化状态。"""

    model_config = _STRICT
    search_id: str
    run_id: str
    root_question: str
    period: str = "mom"
    period_spec: dict[str, Any] = Field(default_factory=dict)
    root_scope: list[ScopeFilter] = Field(default_factory=list)
    budget: SearchBudget
    nodes: list[SearchNode] = Field(default_factory=list)
    probes: list[ProbeRecord] = Field(default_factory=list)
    rounds: list[RoundRecord] = Field(default_factory=list)
    state: SearchState = "running"
    stop_reason: StopReason | None = None
    final_summary: str = ""
    created_at: str
    updated_at: str
