"""分析运行版本契约（P0 T01）。

一次「Bundle 规划 → 执行 → Dashboard 发布」构成一个不可变运行版本（Run）。
所有下游产物（View 结果 / probe / Dashboard）都携带 run_id 并按运行目录隔离；
当前指针只指向已发布的完整版本，旧运行保留可读身份，不允许跨版本混用。
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RunStatus(str, Enum):
    planned = "planned"      # Bundle 已生成，尚未执行
    executed = "executed"    # 全部 View 已批量执行（允许部分失败）
    published = "published"  # DashboardArtifact 已合成并原子发布


class RunManifest(BaseModel):
    """一次运行的版本身份与口径指纹。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    bundle_id: str = Field(min_length=1)
    status: RunStatus = RunStatus.planned

    # 口径指纹：任一变化都使旧下游失效（禁止只按文件是否存在判断）
    snapshot_hash: str          # 质量快照 parquet 的 sha256
    dictionary_hash: str        # 语义字典 JSON 的 sha256
    metric_spec_version: str = "v1"  # MetricSpec 规则版本
    scope_hash: str             # 分析范围（快照+全局筛选）；无筛选时等于 snapshot_hash
    plan_hash: str              # Bundle 内容（含全部 View Plan）的 sha256

    # P1 T06：固化本运行的全局筛选口径（[{column, values}]）；空列表=全量快照
    scope: list[dict[str, Any]] = Field(default_factory=list)

    created_at: str
    published_at: str | None = None


class RunPointer(BaseModel):
    """运行指针（active=工作流最新运行；current=最近发布的完整版本）。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    bundle_id: str
    updated_at: str
