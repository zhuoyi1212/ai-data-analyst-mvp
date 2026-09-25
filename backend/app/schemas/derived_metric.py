"""确定性派生指标契约（重构 Phase 2，用户第一优先级）。

派生指标（如 Profit Margin = ΣProfit / ΣSales）只能由规则注册、由引擎
derive_ratio 算子在数据快照上确定性计算；LLM 不得直接计算或改写比率值。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DerivedMetricSpec(BaseModel):
    """一个可确定性派生的比率指标定义。"""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)  # 程序内标识，如 profit_margin
    label: str = Field(min_length=1)  # 展示名，如「利润率」
    numerator: str = Field(min_length=1)  # 分子字段（如 Profit）
    denominator: str = Field(min_length=1)  # 分母字段（如 Sales）
    agg: Literal["ratio_of_sums"] = "ratio_of_sums"  # 唯一允许口径
    unit: Literal["%", "x"] = "%"  # 展示单位；引擎产出小数（0.122），前端乘 100
    source: Literal["rule.derived"] = "rule.derived"
