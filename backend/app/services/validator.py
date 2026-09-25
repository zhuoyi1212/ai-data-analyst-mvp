"""Insight Validator 的前置——结果校验器 Validate（FR-7）。

五项确定性检查（全部来自引擎产物与快照的重算，LLM 不参与）：
coverage 覆盖率 / cross_check 交叉验算 / null_handling 空值处理 /
shape 结果形态 / plan_consistency 方案哈希一致性。
fail 阻断可视化与洞察；warn 必须用户知悉后才能继续。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app.schemas.common import CheckLevel
from app.schemas.ledger import Ledger
from app.schemas.plan import PlanArtifact
from app.schemas.validation import CheckItem, ValidationReport
from app.services.engine.ops import run_step
from app.services.planner import plan_hash
from app.services.storage import SessionStore

_TOL = 1e-9


class ValidationGateError(Exception):
    """校验未通过且被阻断，或 warn 尚未知悉。"""


def _item(code, title, ok: bool, detail, numbers=None, warn=False):
    if not ok:
        level = CheckLevel.failed
    elif warn:
        level = CheckLevel.warning
    else:
        level = CheckLevel.passed
    return CheckItem(code=code, title=title, level=level, detail=detail,
                     numbers=numbers or {})


def _check_coverage(ledger: Ledger, plan: PlanArtifact) -> CheckItem:
    removed_filters = sum(
        s.input_rows - s.output_rows for s in ledger.steps if s.op == "filter"
    )
    delta = ledger.snapshot_rows - ledger.participating_rows
    numbers = {"snapshot_rows": ledger.snapshot_rows,
               "participating_rows": ledger.participating_rows,
               "removed_by_filters": removed_filters}
    if delta < 0:
        return _item("coverage", "覆盖率", False,
                     "参与计算行数多于快照行数，结果来源异常。", numbers)
    if delta == 0:
        return _item("coverage", "覆盖率", True,
                     f"全部 {ledger.snapshot_rows} 行数据均参与计算。", numbers)
    if removed_filters == delta:
        return _item(
            "coverage", "覆盖率", True,
            f"共 {ledger.snapshot_rows} 行，筛选条件排除 {delta} 行，"
            f"{ledger.participating_rows} 行进入计算，差额可由筛选步骤解释。",
            numbers,
        )
    return _item("coverage", "覆盖率", False,
                 f"有 {delta} 行未参与计算且无法由筛选条件解释，结果口径存疑。",
                 numbers, warn=True)


def _independent_metric_sum(df: pd.DataFrame, metric: str) -> float:
    return float(pd.to_numeric(df[metric], errors="coerce").sum(skipna=True))


def _check_cross(
    ledger: Ledger, plan: PlanArtifact, terminal_input: pd.DataFrame, result: pd.DataFrame
) -> CheckItem:
    p = plan.plan.steps[-1]
    params = p.params
    nums: dict = {}

    def close(a: float, b: float) -> bool:
        return abs(a - b) <= _TOL * max(1.0, abs(a), abs(b))

    if p.op in {"aggregate"}:
        return _item("cross_check", "交叉验算", True,
                     "单指标结果，无分组/占比分解关系，交叉验算不适用。")
    if p.op == "outlier_flag":
        flagged = int(result["is_outlier"].sum()) if "is_outlier" in result else -1
        ok = flagged == ledger.steps[-1].summary.get("outlier_count")
        return _item("cross_check", "交叉验算", ok,
                     f"独立复算离群点数 {flagged}，与引擎标记数 "
                     f"{ledger.steps[-1].summary.get('outlier_count')} 一致。" if ok else
                     f"离群点数不一致：复算 {flagged}，台账 {ledger.steps[-1].summary.get('outlier_count')}。",
                     {"recounted": flagged})
    if p.op == "top_n":
        return _item("cross_check", "交叉验算", True,
                     f"Top {params.n} 仅包含排名靠前的分组，合计不等于总计属预期。")
    if p.op == "filter":
        return _item("cross_check", "交叉验算", True, "筛选透传步骤，无聚合关系可验算。")

    if p.op == "period_compare":
        s = ledger.steps[-1].summary
        expect = (s["current_value"] - s["previous_value"]) / s["previous_value"] * 100
        ok = close(expect, s["growth_pct"])
        return _item("cross_check", "交叉验算", ok,
                     "用本期/上期值独立反推增长率，与引擎结果一致。" if ok else
                     "增长率与本期/上期数值反推结果不一致。",
                     {"recomputed_growth_pct": expect})
    if p.op == "correlation":
        pairs = terminal_input[[params.column_x, params.column_y]].dropna()
        r = float(np.corrcoef(pairs[params.column_x], pairs[params.column_y])[0, 1])
        ok = close(r, ledger.steps[-1].summary["coefficient"])
        return _item("cross_check", "交叉验算", ok,
                     f"用 numpy 独立复算相关系数 {r:.6f}，与引擎结果一致。" if ok else
                     "相关系数独立复算结果与引擎不一致。",
                     {"recomputed_coefficient": r})

    # group_by / share / compare_groups / time_series：分组合计 vs 独立总计
    if p.op == "time_series":
        if params.func not in {"sum", "count"}:
            return _item("cross_check", "交叉验算", True,
                         f"时间序列 {params.func} 聚合不存在与总计的分解关系，交叉验算不适用。")
        metric = params.metric
        total_ind = float(len(terminal_input)) if params.func == "count" and not metric \
            else _independent_metric_sum(terminal_input, metric)
        total_parts = float(result["value"].sum(skipna=True))
        nums = {"independent_total": total_ind, "parts_total": total_parts}
        ok = close(total_ind, total_parts)
        item = _item("cross_check", "交叉验算", ok,
                     "各周期合计与全量独立汇总一致。" if ok else
                     f"各周期合计 {total_parts:g} 与独立总计 {total_ind:g} 不一致。", nums)
        return item

    if params.func not in {"sum", "count"}:
        return _item("cross_check", "交叉验算", True,
                     f"聚合函数 {params.func} 不存在与总计的分解关系，交叉验算不适用。")

    dim = params.dimension
    metric = params.metric
    if params.func == "count":
        total_ind = float(len(terminal_input))
    else:
        total_ind = _independent_metric_sum(terminal_input, metric)
    total_parts = float(result["value"].sum(skipna=True))
    nums = {"dimension": dim, "independent_total": total_ind, "parts_total": total_parts}
    ok = close(total_ind, total_parts)
    detail = (f"各分组{('「' + dim + '」') if dim else ''}合计 {total_parts:g} "
              f"与独立计算的总计 {total_ind:g} 一致。")
    if not ok:
        detail = f"分组合计 {total_parts:g} 与独立总计 {total_ind:g} 不一致。"
    item = _item("cross_check", "交叉验算", ok, detail, nums)
    if p.op == "share" and ok:
        share_sum = float(result["share"].sum())
        nums["share_sum"] = share_sum
        share_ok = abs(share_sum - 1) <= 1e-6
        if not share_ok:
            return _item("cross_check", "交叉验算", False,
                         f"占比之和为 {share_sum:.6f}，不等于 100%。", nums)
        item = _item("cross_check", "交叉验算", True,
                     f"占比之和 {share_sum:.6f} ≈ 100%，分组合计与总计一致。", nums)
    return item


def _check_nulls(ledger: Ledger) -> CheckItem:
    null_metric = sum(int(s.summary.get("null_excluded", 0)) for s in ledger.steps)
    null_dates = sum(int(s.summary.get("null_dates_excluded", 0)) for s in ledger.steps)
    total = null_metric + null_dates
    numbers = {"null_metric_excluded": null_metric, "null_dates_excluded": null_dates}
    if total == 0:
        return _item("null_handling", "空值处理", True, "计算过程中没有遇到空值。", numbers)
    return _item(
        "null_handling", "空值处理", True,
        f"计算跳过了 {null_metric} 个指标空值与 {null_dates} 个无效日期"
        "（聚合默认跳过空值，不计入分母与样本）。",
        numbers, warn=True,
    )


def _check_shape(ledger: Ledger, plan: PlanArtifact, result: pd.DataFrame) -> CheckItem:
    last_op = plan.plan.steps[-1].op
    inf_count = 0
    nan_by_col: dict[str, int] = {}
    for col in result.columns:
        nums = pd.to_numeric(result[col], errors="coerce") if result[col].dtype == object \
            else result[col]
        if pd.api.types.is_numeric_dtype(nums):
            inf_count += int(np.isinf(nums.to_numpy(dtype=float, na_value=0.0)).sum())
            nan_by_col[str(col)] = int(nums.isna().sum())
    nan_total = sum(nan_by_col.values())
    numbers = {"inf_count": inf_count, "nan_by_column": nan_by_col}

    if "share" in result.columns:
        bad = int(((result["share"] < -1e-6) | (result["share"] > 1 + 1e-6)).sum())
        if bad:
            return _item("shape", "结果形态", False,
                         f"有 {bad} 个占比值超出 0–100% 合法区间。", numbers)
    if inf_count:
        return _item("shape", "结果形态", False,
                     f"结果中出现 {inf_count} 个无穷值，计算不可用。", numbers)
    if nan_total:
        if last_op == "aggregate":
            return _item("shape", "结果形态", False,
                         "聚合结果为空值，无法形成有效结论。", numbers)
        if last_op == "time_series":
            return _item("shape", "结果形态", True,
                         f"时间序列存在 {nan_total} 个无数据周期，图中将以缺口呈现。",
                         numbers, warn=True)
        return _item("shape", "结果形态", True,
                     f"结果中存在 {nan_total} 个空值（可能来自无数据的分组/周期）。",
                     numbers, warn=True)
    return _item("shape", "结果形态", True, "结果无空值/无穷值，百分比（如有）处于合法区间。", numbers)


def _check_plan(ledger: Ledger, plan_artifact: PlanArtifact) -> CheckItem:
    current_hash = plan_hash(plan_artifact.plan)
    ok = (
        plan_artifact.locked
        and current_hash == plan_artifact.plan_hash == ledger.plan_hash
    )
    executed = [s.step_id for s in ledger.steps]
    declared = [s.step_id for s in plan_artifact.plan.steps]
    steps_ok = executed == declared
    numbers = {"ledger_hash": ledger.plan_hash, "confirmed_hash": plan_artifact.plan_hash,
               "recomputed_hash": current_hash}
    return _item("plan_consistency", "方案一致性", ok and steps_ok,
                 "实际执行步骤与已确认方案完全一致，哈希校验通过。" if ok and steps_ok else
                 "实际执行步骤或哈希与已确认方案不一致。", numbers)


def run_validation(session_id: str, store: SessionStore) -> ValidationReport:
    ledger = Ledger.model_validate(store.read_artifact(session_id, "ledger"))
    plan_artifact = PlanArtifact.model_validate(store.read_artifact(session_id, "plan"))
    snapshot = store.load_snapshot(session_id)
    result = pd.read_parquet(store.session_dir(session_id) / "result.parquet")

    # 重放末端算子之前的步骤，得到其真实输入（独立验算口径）
    terminal_input = snapshot
    for step in plan_artifact.plan.steps[:-1]:
        terminal_input = run_step(terminal_input, step).df

    items = [
        _check_coverage(ledger, plan_artifact),
        _check_cross(ledger, plan_artifact, terminal_input, result),
        _check_nulls(ledger),
        _check_shape(ledger, plan_artifact, result),
        _check_plan(ledger, plan_artifact),
    ]
    if any(i.level == CheckLevel.failed for i in items):
        overall = "fail"
    elif any(i.level == CheckLevel.warning for i in items):
        overall = "warn"
    else:
        overall = "pass"

    report = ValidationReport(
        session_id=session_id, overall=overall, items=items,
        acknowledged=False, validated_at=datetime.now(timezone.utc).isoformat(),
    )
    store.write_artifact(session_id, "validation", report.model_dump(mode="json"))
    store.update_meta(session_id, stage="validate", validation_overall=overall)
    return report


def acknowledge(session_id: str, store: SessionStore) -> ValidationReport:
    report = ValidationReport.model_validate(store.read_artifact(session_id, "validation"))
    if report.overall == "fail":
        raise ValidationGateError("校验存在未通过项，不能继续，请调整方案或数据后重新执行。")
    report.acknowledged = True
    store.write_artifact(session_id, "validation", report.model_dump(mode="json"))
    return report


def ensure_consumable(session_id: str, store: SessionStore) -> ValidationReport:
    """可视化/洞察阶段的统一闸门。"""
    report = ValidationReport.model_validate(store.read_artifact(session_id, "validation"))
    if report.overall == "fail":
        raise ValidationGateError("结果校验未通过，已阻断可视化与洞察生成。")
    if report.overall == "warn" and not report.acknowledged:
        raise ValidationGateError("存在需要知悉的校验提示，请确认后再继续。")
    return report
