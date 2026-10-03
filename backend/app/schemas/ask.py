"""追问/下钻契约（Task 8）。

主分析完成后，用户用自然语言继续提问。产物语义：
- 追问在当前 run 的 scope（全局筛选口径）内确定性计算；
- 新视图**追加**进当前 run 的视图集合，原视图内容不变；
- 答案文本中的每个数字必须在新视图 parquet 中接地。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)


class AskClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    evidence_view_ids: list[str] = Field(min_length=1)


class AskArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ask_id: str
    question: str
    answer_claims: list[AskClaim] = Field(min_length=1)
    new_view_ids: list[str] = Field(min_length=1)
    scope_rows: int = Field(ge=0)
    appended_report_section_ids: list[str] = Field(default_factory=list)
    # False：新视图已追加但 Dashboard 重合成失败（保留旧版），前端可提示
    dashboard_refreshed: bool = True
    created_at: str


class AskSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    run_id: str
    asks: list[AskArtifact] = Field(default_factory=list)
