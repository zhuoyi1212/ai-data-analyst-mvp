"""构造给 LLM 的「结果摘要」与给验证器的接地候选（FR-9）。

红线：只含方案口径、聚合后结果（展示精度）与校验状态；
不含全量明细行（相关/离群分析仅给统计摘要），从输入侧杜绝模型编造。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.schemas.ledger import Ledger
from app.schemas.plan import PlanArtifact
from app.schemas.validation import ValidationReport

MAX_DIGEST_ROWS = 50  # 输入载荷中最多包含的聚合行数


def _r2(x: Any) -> float:
    return round(float(x), 2)


def _r1(x: Any) -> float:
    return round(float(x), 1)


def _r3(x: Any) -> float:
    return round(float(x), 3)


@dataclass
class ValuePoint:
    """引擎结果中的一个可引用数值。"""

    step_id: str
    kind: str  # metric / row / summary
    keys: dict[str, Any]
    value: float
    is_percent: bool = False
    label: str = ""


@dataclass
class ResultDigest:
    question: str
    terminal_op: str
    chart: str
    steps: list[dict[str, Any]]
    columns: list[str]
    rows: list[dict[str, Any]]
    result_summary: dict[str, Any]
    points: list[ValuePoint] = field(default_factory=list)
    validation_overall: str = "pass"
    validation_warnings: list[str] = field(default_factory=list)
    participating_rows: int = 0
    risks: list[str] = field(default_factory=list)

    def plain_candidates(self) -> list[float]:
        return [p.value for p in self.points if not p.is_percent]

    def pct_candidates(self) -> list[float]:
        return [p.value for p in self.points if p.is_percent]

    def find_point(self, step_id: str, kind: str, keys: dict[str, Any]) -> ValuePoint | None:
        for p in self.points:
            if p.step_id == step_id and p.kind == kind and _keys_equal(p.keys, keys):
                return p
        return None

    def llm_payload(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "terminal_op": self.terminal_op,
            "steps": self.steps,
            "result_columns": self.columns,
            "result_rows": self.rows,
            "result_summary": self.result_summary,
            "validation_overall": self.validation_overall,
            "validation_warnings": self.validation_warnings,
            "participating_rows": self.participating_rows,
        }


def _keys_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if set(a) != set(b):
        return False
    for k in a:
        if str(a[k]) != str(b[k]):
            return False
    return True


def build_result_digest(
    ledger: Ledger,
    plan_artifact: PlanArtifact,
    result: pd.DataFrame,
    validation: ValidationReport,
) -> ResultDigest:
    last = plan_artifact.plan.steps[-1]
    last_record = ledger.steps[-1]
    steps_view = [
        {"step_id": s.step_id, "op": s.op, "description": s.description, "formula": s.formula}
        for s in ledger.steps
    ]
    warnings = [i.detail for i in validation.items if i.level.value == "warn"]

    digest = ResultDigest(
        question=ledger.question,
        terminal_op=last.op,
        chart=ledger.chart.value,
        steps=steps_view,
        columns=ledger.result_columns,
        rows=[],
        result_summary=dict(last_record.summary),
        validation_overall=validation.overall,
        validation_warnings=warnings,
        participating_rows=ledger.participating_rows,
    )

    sid = last.step_id
    p = last.params

    if last.op == "aggregate":
        v = _r2(last_record.summary["value"])
        digest.points.append(ValuePoint(sid, "metric", {}, v, label="聚合结果"))
        digest.rows = [{"value": v}]

    elif last.op in {"group_by", "top_n", "compare_groups"}:
        dim = p.dimension
        rows = []
        for _, row in result.head(MAX_DIGEST_ROWS).iterrows():
            member = row[dim]
            v = _r2(row["value"])
            key = {dim: str(member)}
            digest.points.append(ValuePoint(sid, "row", key, v, label=str(member)))
            rows.append({dim: str(member), "value": v})
        digest.rows = rows

    elif last.op == "share":
        dim = p.dimension
        rows = []
        for _, row in result.head(MAX_DIGEST_ROWS).iterrows():
            member, v, share = row[dim], _r2(row["value"]), _r1(row["share"] * 100)
            key = {dim: str(member)}
            digest.points.append(ValuePoint(sid, "row", key, v, label=f"{member} 值"))
            digest.points.append(ValuePoint(
                sid, "row", {dim: str(member), "metric": "share_pct"}, share,
                is_percent=True, label=f"{member} 占比"))
            rows.append({dim: str(member), "value": v, "share_pct": share})
        digest.rows = rows

    elif last.op == "time_series":
        date_col = p.date_column
        rows = []
        for _, row in result.head(MAX_DIGEST_ROWS).iterrows():
            label = pd.Timestamp(row[date_col]).strftime("%Y-%m-%d")
            v = _r2(row["value"]) if pd.notna(row["value"]) else None
            digest.points.append(ValuePoint(sid, "row", {date_col: label}, v if v is not None else 0.0,
                                            label=label))
            rows.append({date_col: label, "value": v})
        digest.rows = rows

    elif last.op == "period_compare":
        s = last_record.summary
        cur, prev, growth = _r2(s["current_value"]), _r2(s["previous_value"]), _r1(s["growth_pct"])
        digest.points += [
            ValuePoint(sid, "summary", {"field": "current_value"}, cur, label="本期值"),
            ValuePoint(sid, "summary", {"field": "previous_value"}, prev, label="上期值"),
            ValuePoint(sid, "summary", {"field": "growth_pct"}, growth, is_percent=True,
                       label="变化率"),
        ]
        digest.rows = [{"period": result["period"].iloc[0], "value": cur},
                       {"period": result["period"].iloc[1], "value": prev},
                       {"growth_pct": growth}]

    elif last.op == "correlation":
        s = last_record.summary
        r = _r3(s["coefficient"])
        digest.points += [
            ValuePoint(sid, "summary", {"field": "coefficient"}, r, label="相关系数"),
            ValuePoint(sid, "summary", {"field": "n"}, float(s["n"]), label="样本对数"),
        ]
        digest.rows = []
        digest.result_summary = {"coefficient": r, "n": s["n"], "method": s["method"]}

    elif last.op == "outlier_flag":
        s = last_record.summary
        rate = _r1(s["outlier_rate"] * 100)
        digest.points += [
            ValuePoint(sid, "summary", {"field": "outlier_count"}, float(s["outlier_count"]),
                       label="离群点数"),
            ValuePoint(sid, "summary", {"field": "outlier_rate_pct"}, rate, is_percent=True,
                       label="离群占比"),
            ValuePoint(sid, "summary", {"field": "lower_bound"}, _r2(s["lower_bound"]),
                       label="下界"),
            ValuePoint(sid, "summary", {"field": "upper_bound"}, _r2(s["upper_bound"]),
                       label="上界"),
        ]
        digest.rows = []

    # 可信度风险因素（封顶规则；分组/合计属于描述统计，不按小样本降级）
    if last.op == "correlation":
        digest.risks.append("涉及相关分析，仅反映相关关系")
        n = last_record.summary.get("n")
        if n is not None and n < 30:
            digest.risks.append(f"相关样本量较小（n={n}，少于 30）")
    if last.op == "time_series" and ledger.result_rows_total < 8:
        digest.risks.append(f"时间周期较少（{ledger.result_rows_total} 个周期）")
    if last_record.summary.get("outlier_count", 0):
        digest.risks.append("结果受离群值影响")
    if validation.overall == "warn":
        digest.risks.append("结果校验存在已知悉的 warn 项")

    return digest
