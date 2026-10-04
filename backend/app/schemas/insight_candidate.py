"""Insight Candidate 契约（重构 Phase 2：Insight-first Dashboard）。

InsightCandidate 描述一个「业务事实」（而非图表）：
    Broad Scan 广算 → 提取信号/发现 → 抽象为候选 Insight → 评分排序 →
    为 Insight 选证据视图 → 组织成 Dashboard。

与 Signal/Finding 的区别：
    - Signal  是「可深挖的信号」（八类，带量级与方向，驱动 Diagnostic 深挖）；
    - Finding 是「跨视图结论」（四要素：现象/位置/量化影响/建议）；
    - InsightCandidate 是「面向 Dashboard 的业务事实」，统一视图证据、
      结果驱动评分（影响/可行动/可解释/异常/置信/冗余），并按业务故事排序。

所有数值必须来自引擎计算；InsightCandidate 只做语义聚合与评分，禁止自由数字。
全部模型 extra=forbid。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import ChartType

# 业务事实类型（按业务含义划分，不按图表类型划分）
InsightType = Literal[
    "performance_change",  # 核心指标明显增长 / 下滑（如：10 月销售额环比下降 18.4%）
    "contribution",        # 谁贡献了整体变化（如：华南贡献了本期下降额的 63%）
    "concentration",       # 业务是否过度集中（如：Top 3 商品贡献销售额 71%）
    "underperformance",    # 表现明显落后的对象（如：华南销售额第二但利润率最低）
    "divergence",          # 两个指标之间发生背离（如：销售额增但利润降）
    "anomaly",             # 明显异常点（如：10 月 18 日销售额显著偏离正常区间）
    "efficiency",          # 效率 / 比率问题（如：渠道 A 销售额最高但转化率最低）
    "opportunity",         # 明显增长机会（如：线上渠道连续 3 期增长）
    "risk",                # 明显风险（如：区域销售占比高但连续两期下降）
    "relationship",        # 强相关且具业务解释价值（弱相关不得占主区）
]

InsightImportance = Literal["high", "medium", "low"]


class InsightCandidate(BaseModel):
    """一个业务事实候选。按类型语义，部分字段可空（nullable）。"""

    model_config = ConfigDict(extra="forbid")

    insight_id: str
    type: InsightType
    title: str
    summary: str
    metric: str | None = None
    scope: str = ""                       # 全局筛选口径指纹（Finding 同源）
    dimension: str | None = None
    member: str | None = None
    current_value: float | None = None
    comparison_value: float | None = None
    delta: float | None = None
    delta_pct: float | None = None        # 变化率（小数，如 -0.184 = -18.4%）

    # 结果驱动评分（0..1，详见 insight_candidates.score）
    impact_score: float = 0.0
    anomaly_score: float = 0.0
    actionability_score: float = 0.0
    explainability_score: float = 0.0
    confidence_score: float = 0.0
    redundancy_score: float = 0.0
    final_score: float = 0.0

    evidence_view_ids: list[str] = Field(default_factory=list)
    recommended_chart_type: ChartType | None = None
    importance: InsightImportance = "medium"
    reason: str = ""                      # 得分/隐藏/排序的口径说明（中文）


class InsightCandidateSet(BaseModel):
    """一次运行的全部候选 Insight（合成器输入；final_score 排序后取 top K）。"""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    run_id: str
    candidates: list[InsightCandidate] = Field(default_factory=list)