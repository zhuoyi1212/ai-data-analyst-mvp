"""数据质量处理动作执行与快照生成（FR-3）。

执行顺序固定且可审计：格式转换 → 去重 → 缺失处理 → 离群处理。
所有动作来自 schemas.quality.ACTIONS 白名单，不存在任意操作。
"""
from __future__ import annotations

import pandas as pd

from app.schemas.dictionary import DataDictionary
from app.schemas.quality import IssueDecision, IssueType, QualityReport
from app.services.quality_checker import iqr_mask
from app.services.storage import SessionStore


def _validate_decisions(report: QualityReport, raw_decisions: dict) -> None:
    if not report.issues:
        raise ValueError("当前数据没有待处理的质量问题。")
    by_id = {i.issue_id: i for i in report.issues}
    missing = [i.issue_id for i in report.issues if i.issue_id not in raw_decisions]
    if missing:
        raise ValueError(f"仍有 {len(missing)} 个质量问题未做出处理决策：{missing[:3]} …")
    for issue_id, payload in raw_decisions.items():
        issue = by_id.get(issue_id)
        if issue is None:
            raise ValueError(f"未知的质量问题 ID：{issue_id}")
        action = payload.get("action") if isinstance(payload, dict) else None
        if action not in issue.available_actions:
            raise ValueError(
                f"问题「{issue.title}」不支持动作 {action}，"
                f"可选：{issue.available_actions}"
            )


def _apply_formats(df: pd.DataFrame, decisions: dict[str, IssueDecision]) -> pd.DataFrame:
    for issue_id, d in decisions.items():
        if not issue_id.startswith("format:") or d.action != "convert":
            continue
        col = issue_id.split(":", 2)[2]
        subtype = issue_id.split(":")[1]
        if subtype == "date_text":
            df[col] = pd.to_datetime(df[col], errors="coerce", format="mixed")
        elif subtype in {"currency", "number_string"}:
            cleaned = df[col].astype(str).str.replace(r"[¥￥$£,\s]", "", regex=True)
            df[col] = pd.to_numeric(cleaned.replace({"": pd.NA, "nan": pd.NA}), errors="coerce")
        elif subtype == "enum_case":
            values = df[col].astype("string")
            groups = values.dropna().str.lower()
            # 每个小写组选用出现次数最多的原始写法作为标准值
            canonical: dict[str, str] = {}
            for low in groups.unique():
                variants = values[groups == low]
                counts = variants.value_counts()
                canonical[str(low)] = sorted(counts[counts == counts.max()].index)[0]
            df[col] = groups.map(canonical).astype(object)
        elif subtype == "sentinel":
            df.loc[df[col] == -1, col] = pd.NA
    return df


UNKNOWN_BUCKET = "未知"


def _apply_missing(
    df: pd.DataFrame, decisions: dict[str, IssueDecision],
    imputed: dict[str, int],
) -> pd.DataFrame:
    drop_rows: set = set()
    for issue_id, d in decisions.items():
        if not issue_id.startswith("missing:"):
            continue
        col = issue_id.split(":", 1)[1]
        action = d.action
        if action == "keep":
            continue
        n_missing = int(df[col].isna().sum())
        if action == "drop_rows":
            drop_rows.update(df.index[df[col].isna()].tolist())
            continue
        if action == "fill_unknown":
            # 分类缺失归入显式「未知」桶：行保留、覆盖可统计
            df[col] = df[col].astype("object").fillna(UNKNOWN_BUCKET)
            imputed[col] = imputed.get(col, 0) + n_missing
        elif action == "fill_value":
            value = d.params.get("fill_value")
            if value is None:
                raise ValueError(f"「{col}」选择固定值填充时必须提供 fill_value。")
            df[col] = df[col].fillna(value)
            imputed[col] = imputed.get(col, 0) + n_missing
        elif action == "fill_mean":
            df[col] = df[col].fillna(pd.to_numeric(df[col], errors="coerce").mean())
            imputed[col] = imputed.get(col, 0) + n_missing
        elif action == "fill_median":
            df[col] = df[col].fillna(pd.to_numeric(df[col], errors="coerce").median())
            imputed[col] = imputed.get(col, 0) + n_missing
        elif action == "fill_mode":
            modes = df[col].mode()
            if not modes.empty:
                df[col] = df[col].fillna(modes.iloc[0])
                imputed[col] = imputed.get(col, 0) + n_missing
    if drop_rows:
        df = df.drop(index=list(drop_rows))
    return df


def _apply_outliers(
    df: pd.DataFrame,
    decisions: dict[str, IssueDecision],
    dictionary: DataDictionary,
) -> pd.DataFrame:
    exclude_rows: set = set()
    for issue_id, d in decisions.items():
        if not issue_id.startswith("outlier:"):
            continue
        col = issue_id.split(":", 1)[1]
        result = iqr_mask(df[col])
        if result is None:
            continue
        _, _, mask = result
        if d.action == "keep":
            continue
        if d.action == "mark":
            df[f"{col}_is_outlier"] = mask.reindex(df.index, fill_value=False).astype(bool)
        elif d.action == "exclude":
            exclude_rows.update(df.index[mask].tolist())
    if exclude_rows:
        df = df.drop(index=list(exclude_rows))
    return df


# 会改变经营事实（行数/数值）的动作：演示脚本绝不自动采纳，
# 避免把脚本选择伪装成「用户已确认」（执行清单 T02 明令禁止）。
FACT_CHANGING_ACTIONS = {
    "drop_rows", "drop_duplicates", "exclude",
    "fill_value", "fill_mean", "fill_median", "fill_mode",
}


def safe_demo_decisions(report: QualityReport) -> dict:
    """演示/无人值守路径的安全决策：只转格式，其余保留并明示。"""
    decisions: dict[str, object] = {}
    for issue in report.issues:
        if issue.type.value == "format" and issue.suggested_action == "convert":
            decisions[issue.issue_id] = {"action": "convert"}
        else:
            decisions[issue.issue_id] = {"action": "keep"}
    return decisions


def _numeric_totals(df: pd.DataFrame, dictionary: DataDictionary) -> dict[str, float]:
    """关键数值指标清洗前后总量对比（插补/删行如何改变经营事实一目了然）。"""
    totals: dict[str, float] = {}
    for f in dictionary.fields:
        if f.ignored or f.semantic_type.value != "metric" or f.name not in df.columns:
            continue
        s = pd.to_numeric(df[f.name], errors="coerce").dropna()
        if not s.empty:
            totals[f.name] = float(s.sum())
    return totals


def apply_decisions(session_id: str, raw_decisions: dict, store: SessionStore) -> QualityReport:
    report = QualityReport.model_validate(store.read_artifact(session_id, "quality"))
    dictionary = DataDictionary.model_validate(store.read_artifact(session_id, "dictionary"))

    _validate_decisions(report, raw_decisions)
    decisions = {
        issue_id: IssueDecision(
            issue_id=issue_id,
            action=payload["action"],
            params=payload.get("params", {}),
        )
        for issue_id, payload in raw_decisions.items()
    }

    df = store.load_original(session_id)
    rows_before = int(df.shape[0])
    totals_before = _numeric_totals(df, dictionary)
    imputed_cells: dict[str, int] = {}

    # 1) 格式转换
    df = _apply_formats(df, decisions)
    # 2) 重复行（默认 keep；只有显式 drop_duplicates 才删）
    dup = decisions.get("duplicate:rows")
    deduped_rows = 0
    if dup is not None and dup.action == "drop_duplicates":
        before = int(df.shape[0])
        df = df.drop_duplicates(keep="first")
        deduped_rows = before - int(df.shape[0])
    # 3) 缺失处理
    df = _apply_missing(df, decisions, imputed_cells)
    # 4) 离群处理
    rows_before_outlier = int(df.shape[0])
    df = _apply_outliers(df, decisions, dictionary)
    excluded_rows = rows_before_outlier - int(df.shape[0])

    df = df.reset_index(drop=True)
    snapshot_hash = store.save_snapshot(session_id, df)

    totals_after = _numeric_totals(df, dictionary)
    # 逐指标总量差异：默认安全路径（keep/mark/convert）下应全部为 0
    total_deltas = {
        col: round(totals_after.get(col, 0.0) - tot, 6)
        for col, tot in totals_before.items()
    }
    dropped_rows = rows_before - int(df.shape[0])
    report.impact = {
        "rows_before": rows_before,
        "rows_after": int(df.shape[0]),
        "dropped_rows": dropped_rows,
        "dropped_breakdown": {
            "duplicates": int(deduped_rows),
            "missing_rows": max(dropped_rows - deduped_rows - excluded_rows, 0),
            "outlier_rows": int(excluded_rows),
        },
        "imputed_cells": imputed_cells,
        "totals_before": totals_before,
        "totals_after": totals_after,
        "total_deltas": total_deltas,
        "fact_changing_actions": sorted({
            iid for iid, d in decisions.items()
            if d.action in {
                "drop_rows", "drop_duplicates", "exclude",
                "fill_value", "fill_mean", "fill_median", "fill_mode",
            }
        }),
        "note": (
            "dropped_rows/imputed_cells 展示清洗对经营事实的影响；"
            "默认建议（keep/mark/convert）不会改变任何指标总量。"
        ),
    }

    report.decisions = decisions
    report.complete = True
    report.snapshot_rows = int(df.shape[0])
    report.snapshot_hash = snapshot_hash
    store.write_artifact(session_id, "quality", report.model_dump(mode="json"))
    store.update_meta(session_id, stage="quality", snapshot_hash=snapshot_hash)
    return report
