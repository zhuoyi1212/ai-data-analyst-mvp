"""数据质量检测器：全部规则确定性执行，不调用 LLM（FR-3）。"""
from __future__ import annotations

import re

import pandas as pd

from app.schemas.common import SemanticType
from app.schemas.dictionary import DataDictionary
from app.schemas.quality import (
    ACTIONS,
    IssueSeverity,
    IssueType,
    QualityIssue,
    QualityReport,
)
from app.services.storage import SessionStore

_CURRENCY_RE = re.compile(r"^[¥￥$£\s]*[\d,]+(?:\.\d+)?\s*$")


def _sev(ratio: float) -> IssueSeverity:
    if ratio >= 0.2:
        return IssueSeverity.high
    if ratio >= 0.05:
        return IssueSeverity.medium
    return IssueSeverity.low


def _is_numeric_series(series: pd.Series) -> bool:
    return str(series.dtype) in {"int64", "float64", "Int64", "Float64"}


def _missing_issue(col: str, series: pd.Series, sentinel_rows: list[int]) -> QualityIssue | None:
    n = len(series)
    null_rows = series.index[series.isna()].tolist()
    total = len(null_rows) + len(sentinel_rows)
    if total == 0:
        return None
    sample_rows = (null_rows + sentinel_rows)[:5]
    # 确定性建议：数值列缺失建议均值填充（对聚合影响中性）；
    # 文本/分类列缺失无法归属，建议删除相关行（用户可改为填充指定值或保留）。
    if _is_numeric_series(series):
        suggested, basis = "fill_mean", "数值列缺失，均值填充对总量/均值聚合影响最小"
    else:
        suggested, basis = "drop_rows", "分类/文本列缺失无法归因到任何维度，建议删除相关行"
    return QualityIssue(
        issue_id=f"missing:{col}",
        type=IssueType.missing,
        column=col,
        title=f"「{col}」存在 {total} 个缺失值",
        severity=_sev(total / n),
        evidence={
            "missing_count": len(null_rows),
            "sentinel_count": len(sentinel_rows),
            "ratio": round(total / n, 4),
            "sample_rows": [int(i) for i in sample_rows],
            "sample_values": [None] * min(len(sample_rows), 5),
            "suggestion_basis": basis,
        },
        suggested_action=suggested,
        available_actions=ACTIONS[IssueType.missing],
    )


def _sentinel_rows(series: pd.Series) -> list[int]:
    """指标列中孤立的 -1 哨兵值：存在 -1、占比 ≤5%、且没有比 -1 更小的负值。"""
    if str(series.dtype) not in {"int64", "float64", "Int64", "Float64"}:
        return []
    minus_one = series == -1
    if int(minus_one.sum()) == 0 or minus_one.mean() > 0.05:
        return []
    if int((series < -1).sum()) > 0:
        return []
    return [int(i) for i in series.index[minus_one].tolist()]


def _currency_convert(series: pd.Series) -> pd.Series | None:
    """尝试把货币/带千分位文本列转成数值；不可转换比例高时返回 None。"""
    cleaned = series.astype(str).str.replace(r"[¥￥$£,\s]", "", regex=True)
    non_empty = cleaned[cleaned != ""]
    if non_empty.empty:
        return None
    converted = pd.to_numeric(non_empty, errors="coerce")
    if converted.notna().mean() < 0.9:
        return None
    full = pd.to_numeric(cleaned.replace("", pd.NA), errors="coerce")
    return full


def _format_issues(col: str, series: pd.Series, semantic: SemanticType, physical: str):
    issues: list[QualityIssue] = []
    n = len(series)

    # 1) 文本日期 → 日期
    if semantic == SemanticType.date and physical == "string":
        issues.append(
            QualityIssue(
                issue_id=f"format:date_text:{col}",
                type=IssueType.format_issue,
                column=col,
                subtype="date_text",
                title=f"「{col}」日期以文本存储，需标准化为日期类型",
                severity=IssueSeverity.medium,
                evidence={"sample_values": series.dropna().head(3).tolist()},
                suggested_action="convert",
                available_actions=ACTIONS[IssueType.format_issue],
            )
        )

    # 2) 货币/数值文本 → 数值
    if physical == "string" and semantic == SemanticType.metric:
        converted = _currency_convert(series.dropna())
        if converted is not None:
            raw_samples = series.dropna().head(3).tolist()
            has_symbol = bool(
                series.astype(str).str.contains(r"[¥￥$£,]").any()
            )
            subtype = "currency" if has_symbol else "number_string"
            issues.append(
                QualityIssue(
                    issue_id=f"format:{subtype}:{col}",
                    type=IssueType.format_issue,
                    column=col,
                    subtype=subtype,
                    title=f"「{col}」数值被存为文本（含货币符号/千分位）"
                    if has_symbol
                    else f"「{col}」数值被存为文本",
                    severity=IssueSeverity.medium,
                    evidence={
                        "sample_values": [str(v) for v in raw_samples],
                        "converted_sample": [float(v) for v in converted.head(3)],
                    },
                    suggested_action="convert",
                    available_actions=ACTIONS[IssueType.format_issue],
                )
            )

    # 3) 枚举大小写不一致
    if physical == "string" and semantic in {SemanticType.dimension, SemanticType.geo}:
        values = series.dropna().astype(str)
        lowered = values.str.lower()
        if lowered.nunique() < values.nunique():
            collisions: dict[str, set] = {}
            for raw, low in zip(values, lowered):
                collisions.setdefault(low, set()).add(raw)
            examples = {
                k: sorted(v) for k, v in collisions.items() if len(v) > 1
            }
            if examples:
                first = dict(list(examples.items())[:5])
                issues.append(
                    QualityIssue(
                        issue_id=f"format:enum_case:{col}",
                        type=IssueType.format_issue,
                        column=col,
                        subtype="enum_case",
                        title=f"「{col}」枚举值存在大小写不一致",
                        severity=IssueSeverity.low,
                        evidence={"collisions": {k: list(v) for k, v in first.items()}},
                        suggested_action="convert",
                        available_actions=ACTIONS[IssueType.format_issue],
                    )
                )

    # 4) -1 哨兵值
    sentinel = _sentinel_rows(series)
    if sentinel:
        issues.append(
            QualityIssue(
                issue_id=f"format:sentinel:{col}",
                type=IssueType.format_issue,
                column=col,
                subtype="sentinel",
                title=f"「{col}」存在 {len(sentinel)} 个疑似哨兵值 -1（实际为缺失）",
                severity=IssueSeverity.medium,
                evidence={"sentinel_value": -1, "sample_rows": sentinel[:5], "count": len(sentinel)},
                suggested_action="convert",
                available_actions=ACTIONS[IssueType.format_issue],
            )
        )
    return issues


def iqr_mask(numeric: pd.Series):
    """返回 (lower, upper, mask)；样本不足/无离散时返回 None。检测与处理共用。"""
    s = pd.to_numeric(numeric, errors="coerce").dropna()
    if len(s) < 20 or s.nunique() <= 2:
        return None
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = q3 - q1
    if iqr == 0:
        return None
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return lo, hi, (numeric < lo) | (numeric > hi)


def _outlier_suggestion(
    numeric: pd.Series, mask: pd.Series, lo: float, hi: float
) -> tuple[str, str]:
    """按极端程度/占比/取值域给出确定性的处理建议。

    - 正常值全部非负而离群值为负（违反取值域，如负库存）→ 排除；
    - 离群值超过上界 12 倍（极端录入错误）→ 排除；
    - 超过上界 2 倍但占比 <5%（孤立峰值）→ 标记保留；
    - 其余（轻微越界或占比较高的偏态厚尾）→ 保留。
    """
    valid = pd.to_numeric(numeric, errors="coerce").dropna()
    out = valid[mask.reindex(valid.index, fill_value=False)]
    normal = valid[~mask.reindex(valid.index, fill_value=False)]
    if len(out) == 0 or len(normal) == 0:
        return "keep", "证据不足，默认保留"
    share = len(out) / len(valid)
    out_min, out_max = float(out.min()), float(out.max())
    normal_min = float(normal.min())
    if out_min < 0 and lo < 0 and out_min < lo and normal_min >= 0:
        return "exclude", "离群值为负数而正常值全部非负，违反取值域，建议排除"
    if hi > 0 and out_max / hi >= 12:
        return "exclude", f"最大值约为上界的 {out_max / hi:.0f} 倍，属于极端异常值，建议排除"
    if hi > 0 and out_max / hi >= 2 and share < 0.05:
        return "mark", f"超出上界但占比仅 {share:.1%}，建议标记保留以便识别"
    return "keep", f"越界幅度有限或离群占比 {share:.1%}（偏态厚尾），倾向真实波动，建议保留"


def _outlier_issue(col: str, numeric: pd.Series) -> QualityIssue | None:
    result = iqr_mask(numeric)
    if result is None:
        return None
    lo, hi, mask = result
    count = int(mask.sum())
    if count == 0 or count / len(numeric.dropna()) > 0.15:
        return None
    rows = numeric.index[mask].tolist()[:5]
    values = [float(v) for v in numeric[mask].head(5)]
    suggested, basis = _outlier_suggestion(numeric, mask, float(lo), float(hi))
    return QualityIssue(
        issue_id=f"outlier:{col}",
        type=IssueType.outlier,
        column=col,
        subtype="iqr",
        title=f"「{col}」检测到 {count} 个 IQR 离群值",
        severity=IssueSeverity.low if count / len(numeric) < 0.02 else IssueSeverity.medium,
        evidence={
            "rule": "IQR 1.5 倍",
            "lower_bound": float(lo),
            "upper_bound": float(hi),
            "count": count,
            "sample_rows": [int(i) for i in rows],
            "sample_values": values,
            "suggestion_basis": basis,
        },
        suggested_action=suggested,
        available_actions=ACTIONS[IssueType.outlier],
    )


def run_checks(df: pd.DataFrame, dictionary: DataDictionary) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    field_map = {f.name: f for f in dictionary.fields if not f.ignored}

    for name, fp in field_map.items():
        s = df[name]
        # 缺失（含哨兵计数）
        sentinel = _sentinel_rows(s) if fp.semantic_type == SemanticType.metric else []
        mi = _missing_issue(name, s, sentinel)
        if mi:
            issues.append(mi)
        # 格式类
        issues.extend(_format_issues(name, s, fp.semantic_type, fp.physical_type))
        # 离群：指标数值列（含可转换的文本数值列）；哨兵行不参与离群判定
        numeric = s
        if fp.physical_type == "string" and fp.semantic_type == SemanticType.metric:
            converted = _currency_convert(s.dropna())
            if converted is not None:
                numeric = pd.to_numeric(
                    s.astype(str).str.replace(r"[¥￥$£,\s]", "", regex=True), errors="coerce"
                )
            else:
                continue
        if fp.semantic_type == SemanticType.metric:
            if sentinel:
                numeric = numeric.copy()
                numeric.loc[sentinel] = pd.NA
            oi = _outlier_issue(name, numeric)
            if oi:
                issues.append(oi)

    # 完全重复行
    dup_mask = df.duplicated(keep="first")
    dup_count = int(dup_mask.sum())
    if dup_count:
        issues.append(
            QualityIssue(
                issue_id="duplicate:rows",
                type=IssueType.duplicate,
                title=f"检测到 {dup_count} 行完全重复记录",
                severity=IssueSeverity.medium if dup_count / len(df) < 0.05 else IssueSeverity.high,
                evidence={
                    "extra_rows": dup_count,
                    "ratio": round(dup_count / len(df), 4),
                    "sample_rows": [int(i) for i in df.index[dup_mask][:5]],
                },
                suggested_action="drop_duplicates",
                available_actions=ACTIONS[IssueType.duplicate],
            )
        )

    return issues


def run_quality_checks(session_id: str, store: SessionStore) -> QualityReport:
    dictionary = DataDictionary.model_validate(store.read_artifact(session_id, "dictionary"))
    if not dictionary.complete:
        raise ValueError("数据字典尚未完成用户确认，请先完成语义确认。")
    df = store.load_original(session_id)
    issues = run_checks(df, dictionary)
    report = QualityReport(session_id=session_id, issues=issues)
    store.write_artifact(session_id, "quality", report.model_dump(mode="json"))
    return report
