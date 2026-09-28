"""数据质量报告与处理决策契约（FR-3）。"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class IssueType(str, Enum):
    missing = "missing"  # 缺失值（含哨兵值）
    format_issue = "format"  # 类型/格式异常
    duplicate = "duplicate"  # 完全重复行
    outlier = "outlier"  # 数值离群


class IssueSeverity(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


# 处理动作白名单（与 UI 选项一一对应）
# T02：fill_unknown = 分类缺失归入显式「未知」桶（保留行、明示覆盖影响）；
# 数值指标缺失默认 keep——不默认填均值，插补值不得伪装成真实经营总量。
ACTIONS = {
    IssueType.missing: [
        "keep", "drop_rows", "fill_unknown",
        "fill_value", "fill_mean", "fill_median", "fill_mode",
    ],
    IssueType.format_issue: ["convert", "keep_text"],
    IssueType.duplicate: ["drop_duplicates", "keep"],
    IssueType.outlier: ["keep", "mark", "exclude"],
}


class QualityIssue(BaseModel):
    issue_id: str
    type: IssueType
    column: str | None = None  # 重复行为 None
    subtype: str = ""  # sentinel / currency / date_text / enum_case / iqr ...
    title: str
    severity: IssueSeverity = IssueSeverity.medium
    evidence: dict = Field(default_factory=dict)
    suggested_action: str
    available_actions: list[str]


class IssueDecision(BaseModel):
    issue_id: str
    action: str
    params: dict = Field(default_factory=dict)  # 如 {"fill_value": "未知"}


class QualityReport(BaseModel):
    session_id: str
    issues: list[QualityIssue] = Field(default_factory=list)
    decisions: dict[str, IssueDecision] = Field(default_factory=dict)
    complete: bool = False
    snapshot_rows: int | None = None
    snapshot_hash: str | None = None
    # P0 T02：清洗影响留痕——删除行数、插补单元格、清洗前后关键指标总量，
    # 让「口径如何改变经营事实」在报告里可审。
    impact: dict = Field(default_factory=dict)
