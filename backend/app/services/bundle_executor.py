"""AnalysisBundle 批量执行器（重构 Phase 1）。

可靠性原则：
- 每个 View 独立调用纯计算内核 executor.execute_plan，数值唯一来源仍是白名单算子；
- partial success：单个 View 失败（引擎错误/写盘错误）只标记该 View 失败并给中文原因，
  其余 View 继续执行，汇总为 BundleExecutionResult；
- 每个 View 的台账/结果/校验写入独立目录 bundle/views/{view_id}/，互不覆盖；
- View 级轻量校验（coverage / shape / null_handling）随结果落盘，供证据链与
  Phase 2 Dashboard 合成使用。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from app.config import settings
from app.schemas.bundle import (
    AnalysisBundle,
    BundleExecutionResult,
    AnalysisView,
    ViewExecutionResult,
    ViewStatus,
    ViewType,
    ViewValidationItem,
)
from app.schemas.ledger import Ledger
from app.services.engine.ops import EngineError
from app.services.executor import clean_rows, execute_plan
from app.services.planner import plan_hash
from app.services.storage import SessionStore


def _snapshot_hash(snap_path: Path) -> str:
    return hashlib.sha256(snap_path.read_bytes()).hexdigest()


def _participating_rows(records, snapshot_rows: int) -> int:
    """进入末端计算的行数：取最后一个 filter 的输出；无筛选即整张快照。"""
    filters = [r for r in records if r.op == "filter"]
    if filters:
        return int(filters[-1].output_rows)
    return int(records[-1].input_rows) if records else snapshot_rows


def _checks(
    view: AnalysisView, result: pd.DataFrame, participating_rows: int
) -> list[ViewValidationItem]:
    checks: list[ViewValidationItem] = []

    # coverage（T04）：执行成功 ≠ 结果有效。零行是 no_data 而非 fail，
    # 但两者都不可消费；展示层必须显示「无数据」，严禁把 KPI 当 0。
    if participating_rows > 0 and len(result.index) > 0:
        checks.append(ViewValidationItem(
            code="coverage", level="pass",
            detail=f"参与计算 {participating_rows} 行，输出 {len(result.index)} 行",
            numbers={"participating_rows": participating_rows,
                     "result_rows": len(result.index)},
        ))
    else:
        checks.append(ViewValidationItem(
            code="coverage", level="no_data",
            detail=(
                f"筛选后无有效数据（参与 {participating_rows} 行，"
                f"输出 {len(result.index)} 行），该视角不进入仪表盘，也不显示为 0"
            ),
            numbers={"participating_rows": participating_rows,
                     "result_rows": len(result.index)},
        ))

    # shape：数值列不允许全 NaN / Inf（零分母产生的 null 是「不可计算」，
    # 由 null_handling 单独计数，不算非法结构）
    numeric = result.select_dtypes(include=[np.number])
    bad_cols = [
        str(c) for c in numeric.columns
        if len(numeric[c].dropna()) == 0 or np.isinf(numeric[c].dropna()).any()
    ]
    if bad_cols:
        checks.append(ViewValidationItem(
            code="shape", level="fail",
            detail=f"结果数值列存在全空或无穷值：{'、'.join(bad_cols)}",
            numbers={"bad_columns": bad_cols},
        ))
    else:
        checks.append(ViewValidationItem(
            code="shape", level="pass",
            detail="结果结构正常，无全空/无穷数值列",
            numbers={"numeric_columns": [str(c) for c in numeric.columns]},
        ))

    # null_handling：结果指标列的空值计数（有则 warn，明示而非隐瞒）
    null_counts = {
        str(c): int(numeric[c].isna().sum())
        for c in numeric.columns
        if int(numeric[c].isna().sum()) > 0
    }
    if null_counts:
        checks.append(ViewValidationItem(
            code="null_handling", level="warn",
            detail="结果中存在空值（可能为分母为 0 的「不可计算」），展示需明示",
            numbers={"null_counts": null_counts},
        ))
    else:
        checks.append(ViewValidationItem(
            code="null_handling", level="pass", detail="结果无空值", numbers={},
        ))

    # reconciliation（T04）：份额回总必须 ≈1，否则说明口径/过滤不一致
    if "share" in result.columns and pd.api.types.is_numeric_dtype(result["share"]):
        share_sum = float(result["share"].sum())
        if np.isnan(share_sum) or abs(share_sum - 1.0) > 1e-6:
            checks.append(ViewValidationItem(
                code="reconciliation", level="warn",
                detail=f"各分组份额合计为 {share_sum:.6f}，不等于 100%，请核口径",
                numbers={"share_sum": share_sum},
            ))
        else:
            checks.append(ViewValidationItem(
                code="reconciliation", level="pass",
                detail="各分组份额合计为 100%，与总体回总一致",
                numbers={"share_sum": share_sum},
            ))
    return checks


def _view_preview(view: AnalysisView, result: pd.DataFrame, limit: int) -> list[dict]:
    """View 类型感知的结果预览（全量结果仍原样写 parquet，预览只影响展示口径）。

    anomaly：head() 恰好可能全是非离群行，预览改为「被标记的离群行优先，
    按被检测指标降序」，没有离群行时才退回普通 head。
    """
    if view.type is ViewType.anomaly and "is_outlier" in result.columns:
        flagged = result[result["is_outlier"]]
        col = view.metric_fields[0] if view.metric_fields else None
        if col and col in flagged.columns:
            flagged = flagged.sort_values(col, ascending=False)
        if not flagged.empty:
            return clean_rows(flagged, limit)
    return clean_rows(result, limit)


def _execute_one(
    session_id: str,
    store: SessionStore,
    view,  # AnalysisView
    snapshot: pd.DataFrame,
    snapshot_rows: int,
    snap_hash: str,
) -> ViewExecutionResult:
    """执行单个 View 并落盘；任何异常收敛为 status=failed（不抛出）。"""
    try:
        result, records, elapsed_ms = execute_plan(snapshot, view.plan)
    except EngineError as e:
        return ViewExecutionResult(
            view_id=view.view_id, status=ViewStatus.failed,
            reason=f"计算引擎拒绝该方案：{e}", chart=view.chart,
            snapshot_rows=snapshot_rows,
        )
    except Exception as e:  # noqa: BLE001 —— View 级隔离：任何意外都不得拖垮批量执行
        return ViewExecutionResult(
            view_id=view.view_id, status=ViewStatus.failed,
            reason=f"执行失败：{type(e).__name__}: {e}", chart=view.chart,
            snapshot_rows=snapshot_rows,
        )

    participating_rows = _participating_rows(records, snapshot_rows)
    checks = _checks(view, result, participating_rows)

    # 落盘：结果 parquet + 台账 + 校验（写盘失败同样收敛为 failed）
    try:
        store.save_view_result(session_id, view.view_id, result)
        ledger = Ledger(
            session_id=session_id,
            question=view.question,
            data_scope=view.plan.data_scope,
            plan_hash=plan_hash(view.plan),
            snapshot_hash=snap_hash,
            snapshot_rows=snapshot_rows,
            participating_rows=participating_rows,
            chart=view.chart,
            steps=records,
            result_columns=[str(c) for c in result.columns],
            result_rows_total=len(result.index),
            result_preview=_view_preview(view, result, settings.table_display_rows),
            elapsed_ms=elapsed_ms,
            executed_at=datetime.now(timezone.utc).isoformat(),
        )
        store.write_view_ledger(session_id, view.view_id, ledger.model_dump(mode="json"))
        store.write_view_validation(
            session_id, view.view_id,
            {"view_id": view.view_id, "checks": [c.model_dump(mode="json") for c in checks]},
        )
    except Exception as e:  # noqa: BLE001
        return ViewExecutionResult(
            view_id=view.view_id, status=ViewStatus.failed,
            reason=f"结果落盘失败：{type(e).__name__}: {e}", chart=view.chart,
            snapshot_rows=snapshot_rows,
        )

    return ViewExecutionResult(
        view_id=view.view_id,
        status=ViewStatus.success,
        chart=view.chart,
        steps=records,
        result_columns=[str(c) for c in result.columns],
        result_rows_total=len(result.index),
        result_preview=_view_preview(view, result, settings.table_display_rows),
        participating_rows=participating_rows,
        snapshot_rows=snapshot_rows,
        elapsed_ms=elapsed_ms,
        checks=checks,
    )


def execute_bundle(
    session_id: str, store: SessionStore, bundle: AnalysisBundle
) -> BundleExecutionResult:
    """批量执行 Bundle 全部 View（partial success），结果落盘并返回汇总。"""
    snapshot = store.load_snapshot(session_id)
    quality = store.read_artifact(session_id, "quality")
    snap_hash = quality["snapshot_hash"]
    # 以实际快照为准（quality 中的行数同时留痕在各 View 台账）
    actual_hash = _snapshot_hash(store.session_dir(session_id) / "snapshot.parquet")
    if snap_hash != actual_hash:
        raise ValueError("数据快照与质量处理记录不一致，请重新完成数据质量处理。")

    # T01：执行必须锚定当前运行版本；Bundle 身份/快照指纹任一不符即拒
    manifest = store.run_manifest(session_id)
    if bundle.bundle_id != manifest.bundle_id:
        from app.services.storage import StaleRunError
        raise StaleRunError(
            "要执行的分析蓝图不是当前运行版本，请重新生成并执行 AnalysisBundle。"
        )
    if manifest.snapshot_hash != actual_hash:
        from app.services.storage import StaleRunError
        raise StaleRunError("数据快照已变更，当前分析运行失效，请重新生成分析蓝图。")

    results = [
        _execute_one(session_id, store, view, snapshot, len(snapshot.index), snap_hash)
        for view in bundle.analysis_views
    ]
    summary = BundleExecutionResult(
        run_id=manifest.run_id,
        bundle_id=bundle.bundle_id,
        total=len(results),
        succeeded=sum(1 for r in results if r.status == ViewStatus.success),
        failed=sum(1 for r in results if r.status == ViewStatus.failed),
        views=results,
    )
    store.write_bundle_execution(session_id, summary.model_dump(mode="json"))
    return summary
