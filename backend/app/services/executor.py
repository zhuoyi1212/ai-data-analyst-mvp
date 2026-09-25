"""Analysis Executor：在质量快照上执行锁定方案并产出计算台账（FR-6）。

- 只执行已确认锁定的方案，执行前复核方案哈希（防篡改）；
- 数值全部来自 app.services.engine.ops，LLM 不参与本阶段；
- 成功才落盘台账与全量结果；任何引擎错误直接中文阻断，不写结果。
"""
from __future__ import annotations

import hashlib
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import settings
from app.schemas.ledger import Ledger, StepRecord
from app.schemas.plan import AnalysisPlan, PlanArtifact
from app.services.engine.ops import EngineError, run_step
from app.services.parser import json_safe
from app.services.planner import plan_hash
from app.services.storage import SessionStore


class ExecutionGateError(Exception):
    """执行前置闸门失败（方案未锁定/快照缺失/哈希不一致）。"""


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clean_rows(df: pd.DataFrame, limit: int) -> list[dict[str, Any]]:
    preview = df.head(limit)
    rows: list[dict[str, Any]] = []
    records = preview.to_dict(orient="records")
    for row in records:
        clean = {}
        for k, v in row.items():
            if isinstance(v, pd.Timestamp):
                v = v.isoformat()
            elif isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                v = None
            else:
                v = json_safe(v)
            clean[str(k)] = v
        rows.append(clean)
    return rows


def execute(session_id: str, store: SessionStore) -> Ledger:
    # 1) 闸门：方案存在且锁定
    if not store.has_artifact(session_id, "plan"):
        raise ExecutionGateError("尚未生成分析方案。")
    artifact = PlanArtifact.model_validate(store.read_artifact(session_id, "plan"))
    if not artifact.locked or not artifact.plan_hash:
        raise ExecutionGateError("方案尚未确认，请先确认分析方案后再执行。")
    if plan_hash(artifact.plan) != artifact.plan_hash:
        raise ExecutionGateError("方案内容与确认时不一致（哈希校验失败），请重新确认方案。")

    # 2) 闸门：质量处理后快照存在，且哈希与质量报告一致
    try:
        snapshot = store.load_snapshot(session_id)
    except Exception as exc:
        raise ExecutionGateError("数据质量处理尚未完成，缺少可计算的数据快照。") from exc
    quality = store.read_artifact(session_id, "quality")
    snap_path = store.session_dir(session_id) / "snapshot.parquet"
    snapshot_hash = _hash_file(snap_path)
    if quality.get("snapshot_hash") != snapshot_hash:
        raise ExecutionGateError("数据快照与质量处理记录不一致，请重新完成数据质量处理。")

    # 3) 逐步执行（数值唯一来源：引擎算子）
    started = time.perf_counter()
    current = snapshot
    records: list[StepRecord] = []
    try:
        for step in artifact.plan.steps:
            input_rows = len(current)
            result = run_step(current, step)
            records.append(StepRecord(
                step_id=step.step_id,
                op=step.op,
                description=step.description,
                params=step.params.model_dump(mode="json"),
                input_rows=input_rows,
                output_rows=len(result.df),
                output_columns=[str(c) for c in result.df.columns],
                formula=result.formula,
                summary=result.summary,
            ))
            current = result.df
    except EngineError:
        raise  # 中文错误直接阻断；不写台账、不写结果
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    # 4) 全量结果落 parquet；台账仅带预览行
    result_path = store.session_dir(session_id) / "result.parquet"
    current.to_parquet(result_path, index=False)

    ledger = Ledger(
        session_id=session_id,
        question=artifact.question,
        data_scope=artifact.plan.data_scope,
        plan_hash=artifact.plan_hash,
        snapshot_hash=snapshot_hash,
        snapshot_rows=len(snapshot),
        participating_rows=records[-1].input_rows,
        chart=artifact.chart,
        steps=records,
        result_columns=[str(c) for c in current.columns],
        result_rows_total=len(current),
        result_preview=_clean_rows(current, settings.table_display_rows),
        elapsed_ms=elapsed_ms,
        executed_at=datetime.now(timezone.utc).isoformat(),
    )
    store.write_artifact(session_id, "ledger", ledger.model_dump(mode="json"))
    store.update_meta(session_id, stage="execute")
    return ledger
