"""AnalysisReportArtifact 深度分析报告契约（Task 6）。

八节固定结构；每条 claim 必须引用真实计算的 evidence（view/probe id，
经 evidence graph 解析）。LLM 只负责组织论证，数字一律来自确定性引擎。
全部模型 extra="forbid"。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_STRICT = ConfigDict(extra="forbid")

# 固定八节
REPORT_SECTION_IDS = [
    "exec_summary", "performance", "drivers", "risks",
    "diagnostics", "opportunities", "recommendations", "methodology",
]

ReportSectionId = Literal[
    "exec_summary", "performance", "drivers", "risks",
    "diagnostics", "opportunities", "recommendations", "methodology",
]

ClaimType = Literal[
    "fact",        # 事实：KPI/趋势/占比
    "signal",      # 检测到的信号
    "hypothesis",  # 待验证假设（不表述为因果）
    "conclusion",  # 综合结论
]


class ReportClaim(BaseModel):
    """一条论断：文本 + 类型 + 证据引用 + 局限说明。"""

    model_config = _STRICT

    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    claim_type: ClaimType
    evidence_view_ids: list[str] = Field(min_length=1)
    limitations: str = ""


class ReportSection(BaseModel):
    """报告中的一个固定章节。"""

    model_config = _STRICT

    section_id: ReportSectionId
    title: str = Field(min_length=1)
    claims: list[ReportClaim] = Field(default_factory=list)


class AnalysisReportArtifact(BaseModel):
    """深度分析报告产物（随 run 版本化；记录 evidence graph 版本时间）。"""

    model_config = _STRICT

    session_id: str
    run_id: str
    sections: list[ReportSection]
    evidence_graph_created_at: str
    generated_at: str
