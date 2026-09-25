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


def _apply_missing(
    df: pd.DataFrame, decisions: dict[str, IssueDecision]
) -> pd.DataFrame:
    drop_rows: set = set()
    for issue_id, d in decisions.items():
        if not issue_id.startswith("missing:"):
            continue
        col = issue_id.split(":", 1)[1]
        action = d.action
        if action == "keep":
            continue
        if action == "drop_rows":
            drop_rows.update(df.index[df[col].isna()].tolist())
            continue
        if action == "fill_value":
            value = d.params.get("fill_value")
            if value is None:
                raise ValueError(f"「{col}」选择固定值填充时必须提供 fill_value。")
            df[col] = df[col].fillna(value)
        elif action == "fill_mean":
            df[col] = df[col].fillna(pd.to_numeric(df[col], errors="coerce").mean())
        elif action == "fill_median":
            df[col] = df[col].fillna(pd.to_numeric(df[col], errors="coerce").median())
        elif action == "fill_mode":
            modes = df[col].mode()
            if not modes.empty:
                df[col] = df[col].fillna(modes.iloc[0])
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

    # 1) 格式转换
    df = _apply_formats(df, decisions)
    # 2) 重复行
    dup = decisions.get("duplicate:rows")
    if dup is not None and dup.action == "drop_duplicates":
        df = df.drop_duplicates(keep="first")
    # 3) 缺失处理
    df = _apply_missing(df, decisions)
    # 4) 离群处理
    df = _apply_outliers(df, decisions, dictionary)

    df = df.reset_index(drop=True)
    snapshot_hash = store.save_snapshot(session_id, df)

    report.decisions = decisions
    report.complete = True
    report.snapshot_rows = int(df.shape[0])
    report.snapshot_hash = snapshot_hash
    store.write_artifact(session_id, "quality", report.model_dump(mode="json"))
    store.update_meta(session_id, stage="quality", snapshot_hash=snapshot_hash)
    return report
