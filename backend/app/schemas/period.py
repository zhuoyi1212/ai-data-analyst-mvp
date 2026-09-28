"""可比时间口径契约（P0 T05）。

PeriodSpec 显式记录一次同/环比所用的当前窗口、对比窗口、粒度、数据截至日
与完整性状态；KPI value/change/Finding 必须共享同一 PeriodSpec。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

# complete  = 当前期已自然结束且窗口完整；
# partial   = 截至日未到期末（MTD/YTD），只能与等长同期窗口比较；
# unknown   = 覆盖无法验证（max(event_time) 只是线索），不得触发完整周期告警。
Completeness = Literal["complete", "partial", "unknown"]

ComparisonMode = Literal["mom", "wow", "yoy"]


class PeriodSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ComparisonMode
    granularity: Literal["day", "week", "month"]
    timezone: str = "local"

    current_start: str
    current_end: str          # 含端点
    compare_start: str
    compare_end: str          # 含端点，partial 时与当前窗口等长

    data_as_of: str           # max(event_time)：仅线索，非已验证截至日
    completeness: Completeness
    observed_days: int
    expected_days: int

    def model_dump_period(self) -> dict:
        return self.model_dump(mode="json")
