"""自动分析编排契约（Autonomous Analyst）。

AutoAnalysisState 是"一键分析"的持久化状态机：
    profile → quality → scan → signals → diagnostic → synthesis → done

- 阶段状态全部落盘（auto.json），中断后续跑、已完成阶段不重复；
- needs_input：仅在关键歧义/无指标等可由用户作答解除的场景出现；
- 零有效行等不可作答的严重问题直接 failed，并给出中文原因。
全部模型 extra=forbid。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

_STRICT = ConfigDict(extra="forbid")

# ----------------------------------------------------------- Signal 契约


# 信号类型（从 scan 真实数据提取，LLM 不参与判定）
_SIGNAL_TYPES = Literal[
    "growth",            # 显著增长
    "decline",           # 显著下滑
    "negative_member",   # 亏损成员
    "divergence",        # 量利份额背离
    "scale_profit",      # 增收不增利
    "anomaly",           # 离群异常
    "correlation",       # 显著相关
    "simpson",           # Simpson 结构悖论
]

SignalDirection = Literal["up", "down", "negative", "mixed"]


class Magnitude(BaseModel):
    """信号量级：字段全部引用 scan view 的真实计算值（禁止自由数字）。"""

    model_config = _STRICT

    metric: str
    value: float | None = None          # 成员值 / 当前聚合值
    compare_value: float | None = None  # 对照聚合值
    change_pct: float | None = None     # 变化率（百分比数值，如 12.5 表示 12.5%）
    gap_pp: float | None = None         # 份额缺口（百分点）
    coefficient: float | None = None    # 相关系数
    n: int | None = None                # 样本数


class ScoreBreakdown(BaseModel):
    """多维评分明细（口径对齐 T09：影响/集中度/新异性/支撑度）。"""

    model_config = _STRICT

    impact: float
    concentration: float
    novelty: float
    support: float


class Signal(BaseModel):
    """一个从 scan 结果中提取的可深挖信号。"""

    model_config = _STRICT

    signal_id: str
    type: _SIGNAL_TYPES
    scan_view_ids: list[str]
    metric: str
    dimension: str | None = None
    member: str | None = None
    direction: SignalDirection
    magnitude: Magnitude
    score: float
    score_breakdown: ScoreBreakdown


class SignalSet(BaseModel):
    """信号阶段产物：全量信号 + 排序后 top K（供 Diagnostic 阶段消费）。"""

    model_config = _STRICT

    session_id: str
    run_id: str
    signals: list[Signal] = Field(default_factory=list)
    top_signal_ids: list[str] = Field(default_factory=list)


# ----------------------------------------------------------- Chain 契约


class ChainNode(BaseModel):
    """深挖链上的一个证据节点：引用真实 scan view 或 diagnostic probe。"""

    model_config = _STRICT

    node_id: str
    parent_id: str | None = None
    # root=信号原证据 / drill=子维度下钻 / cross=交叉·变化贡献 / driver=率·驱动验证
    kind: Literal["root", "drill", "cross", "driver", "validate"]
    ref_type: Literal["view", "probe"]
    ref_id: str
    dimension: str | None = None
    member: str | None = None
    scope_filters: list[dict[str, Any]] = Field(default_factory=list)
    depth: int = Field(ge=0)
    scores: dict[str, Any] | None = None
    status: Literal["completed", "stopped"]


class DiagnosticViewRef(BaseModel):
    """深挖产出的可展示证据（probe 内某个已执行终端计划）。"""

    model_config = _STRICT

    probe_id: str
    op: str
    dimension: str
    metric: str
    label: str


class AnalysisChain(BaseModel):
    """以一个 top 信号为根的完整深挖链。"""

    model_config = _STRICT

    chain_id: str
    root_signal_id: str
    root_question: str
    nodes: list[ChainNode] = Field(min_length=1)
    diagnostic_views: list[DiagnosticViewRef] = Field(default_factory=list)
    conclusion: str
    depth: int = Field(ge=0)
    status: Literal["completed", "stopped"]
    stop_reason: str
    budget_used: dict[str, int]


class ChainSet(BaseModel):
    """Diagnostic 阶段产物：多 root 深挖链集合 + 全局预算结算。"""

    model_config = _STRICT

    session_id: str
    run_id: str
    chains: list[AnalysisChain] = Field(default_factory=list)
    budget: dict[str, int]
    created_at: str


# ----------------------------------------------------------- Evidence Graph


class EvidenceNode(BaseModel):
    """证据图谱节点：view/probe/signal/chain/claim。"""

    model_config = _STRICT

    node_id: str
    type: Literal["view", "probe", "signal", "chain", "claim"]
    label: str = ""


class EvidenceEdge(BaseModel):
    """类型化有向边：source → target。

    evidence=view/probe→signal，root=signal→chain，
    uses=chain→view/probe，supports=view/probe→claim。
    """

    model_config = _STRICT

    source: str
    target: str
    type: Literal["evidence", "root", "uses", "supports"]


class EvidenceGraph(BaseModel):
    """证据索引：scan view → signal → chain → 证据 view/probe（→ Task 6 claim）。"""

    model_config = _STRICT

    session_id: str
    run_id: str
    nodes: list[EvidenceNode] = Field(default_factory=list)
    edges: list[EvidenceEdge] = Field(default_factory=list)
    created_at: str


# ----------------------------------------------------------- 编排状态


# 顺序即状态机顺序；"done" 是终止态（不作为 StageLog）
AUTO_STAGES = [
    "profile", "quality", "scan", "signals", "diagnostic", "synthesis",
]

AutoStageName = Literal[
    "profile", "quality", "scan", "signals", "diagnostic", "synthesis",
]

StageStatus = Literal["pending", "running", "completed", "skipped", "blocked"]

AutoRunStatus = Literal["running", "waiting_input", "completed", "failed"]


class StageLog(BaseModel):
    """单个阶段的执行记录（前端进度页按此渲染）。"""

    model_config = _STRICT

    stage: AutoStageName
    status: StageStatus = "pending"
    message: str = ""
    started_at: str = ""
    ended_at: str = ""


class GateQuestion(BaseModel):
    """向用户提出的最小化问题；options 的值即作答值（自由文本走 free_text）。"""

    model_config = _STRICT

    question_id: str
    stage: AutoStageName
    prompt: str
    options: list[str] = Field(default_factory=list)
    allow_free_text: bool = True
    context: dict[str, str] = Field(default_factory=dict)


class AutoAnalysisState(BaseModel):
    """一次自动分析的完整状态（会话级，非 run 级；run 创建后记录 run_id）。"""

    model_config = _STRICT

    session_id: str
    status: AutoRunStatus = "running"
    current_stage: AutoStageName = "profile"
    stages: list[StageLog] = Field(default_factory=list)
    needs_input: list[GateQuestion] = Field(default_factory=list)
    answers: dict[str, str] = Field(default_factory=dict)
    run_id: str = ""
    artifacts: dict[str, str] = Field(default_factory=dict)
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


class AutoAnswerRequest(BaseModel):
    """POST /auto-analyze 的可选请求体：answers 以 question_id 为键。"""

    model_config = _STRICT

    answers: dict[str, str] = Field(default_factory=dict)
