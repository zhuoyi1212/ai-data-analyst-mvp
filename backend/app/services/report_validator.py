"""报告校验器（Task 6）：纯规则门禁，不调用 LLM。

四类检查：
1. 八节齐全、claim_id 唯一；
2. 证据存在性：evidence_view_ids 经 evidence graph 解析为真实 view/probe；
3. 数字接地：claim 文本中的每个数字能在引用证据的结果数据中按精度找到；
4. 因果护栏：复用 insight_validator 的因果词表与免责豁免。
"""
from __future__ import annotations

import re

import pandas as pd

from app.schemas.auto import EvidenceGraph
from app.schemas.report import AnalysisReportArtifact, REPORT_SECTION_IDS
from app.services.insight_validator import _extract_numbers, find_causal_terms
from app.services.storage import SessionStore

# 非业务度量的数值列（ID/标志位/年份等不得给 claim 接地）
_NON_METRIC = re.compile(
    r"(^id$|_id$|(^|_)index$|(^|_)is_|^is_|^flag|outlier_|_year$|^year$|^n$|_code$|^code$)",
    re.IGNORECASE,
)
_PERCENT_COL = re.compile(r"share|pct|percent|rate", re.IGNORECASE)


def _evidence_paths(
    store: SessionStore, sid: str, run_id: str, ref: str, ref_type: str
) -> list:
    if ref_type == "view":
        return [store.view_dir(sid, ref) / "result.parquet"]
    pdir = store.run_dir(sid, run_id) / "diagnostic" / "probes" / ref
    return list(pdir.glob("result_*.parquet"))


def read_value_columns(
    store: SessionStore, sid: str, run_id: str, ref: str, ref_type: str,
) -> dict[str, tuple[float, ...]]:
    """读取证据结果，按列返回数值（同名列合并），供分列接地。"""
    cols: dict[str, list[float]] = {}
    for p in _evidence_paths(store, sid, run_id, ref, ref_type):
        if not p.exists():
            continue
        df = pd.read_parquet(p)
        for col in df.columns:
            nums = pd.to_numeric(df[col], errors="coerce").dropna()
            cols.setdefault(col, []).extend(
                round(float(x), 2) for x in nums
            )
    return {k: tuple(v) for k, v in cols.items()}


def is_metric_col(col: str) -> bool:
    return col == "value" or not _NON_METRIC.search(col)


def close_value(a: float, b: float) -> bool:
    """相对+绝对混合容差：比率等小数值严格，大值允许极小比例误差。"""
    return abs(a - b) <= max(0.01, 0.005 * abs(b)) + 1e-9


def read_string_tokens(
    store: SessionStore, sid: str, run_id: str, ref: str, ref_type: str,
) -> set[int]:
    """读取证据字符串单元格中的整数 token（如维度标签 sub_3 → {3}）。"""
    tokens: set[int] = set()
    for p in _evidence_paths(store, sid, run_id, ref, ref_type):
        if not p.exists():
            continue
        df = pd.read_parquet(p)
        for col in df.columns:
            if pd.api.types.is_numeric_dtype(df[col]):
                continue
            for cell in df[col].dropna().astype(str):
                tokens.update(int(t) for t in re.findall(r"\d+", cell))
    return tokens


def label_token_ok(number: float, text: str, tokens: set[int]) -> bool:
    """数字是否为 claim 直接引用的维度标签 token（形如 dim=sub_3 中的 3）。"""
    if not float(number).is_integer():
        return False
    n = int(number)
    if n not in tokens:
        return False
    return re.search(rf"=[^\s，。（）]*{n}(?![\d.])", text) is not None


def read_value_pool(
    store: SessionStore,
    sid: str,
    run_id: str,
    ref: str,
    ref_type: str,
) -> tuple[float, ...]:
    """读取证据中全部「度量语义列」的数值（向后兼容的扁平池）。"""
    columns = read_value_columns(store, sid, run_id, ref, ref_type)
    values: list[float] = []
    for col, pool in columns.items():
        if is_metric_col(col):
            values.extend(pool)
    return tuple(values)


def grounded(
    value: float, *, is_percent: bool, columns: dict[str, tuple[float, ...]]
) -> bool:
    metric_cols = {c: p for c, p in columns.items() if is_metric_col(c)}
    candidates = [value]
    if is_percent:
        # 百分比文本（12）既可能以 0–1 比率（0.12）也可能以百分数（12.0）存储
        candidates.append(value / 100.0)
        percent_cols = {
            c: p for c, p in metric_cols.items() if _PERCENT_COL.search(c)
        }
        if percent_cols:
            metric_cols = percent_cols
    for pool in metric_cols.values():
        if any(
            close_value(candidate, x)
            for candidate in candidates for x in pool
        ):
            return True
    return False


def validate_report(
    report: AnalysisReportArtifact,
    *,
    session_id: str,
    store: SessionStore,
    graph: EvidenceGraph,
) -> list[str]:
    violations: list[str] = []

    # 1) 八节齐全且不重复
    section_ids = [s.section_id for s in report.sections]
    if sorted(section_ids) != sorted(REPORT_SECTION_IDS):
        missing = set(REPORT_SECTION_IDS) - set(section_ids)
        if missing:
            violations.append(f"报告缺少章节：{sorted(missing)}")
        dupes = {x for x in section_ids if section_ids.count(x) > 1}
        if dupes:
            violations.append(f"报告章节重复：{sorted(dupes)}")

    node_types = {n.node_id: n.type for n in graph.nodes}
    col_cache: dict[str, dict[str, tuple[float, ...]]] = {}
    token_cache: dict[str, set[int]] = {}

    def columns_for(ref: str) -> dict[str, tuple[float, ...]]:
        if ref not in col_cache:
            ref_type = node_types.get(ref)
            col_cache[ref] = (
                read_value_columns(
                    store, session_id, report.run_id, ref, ref_type
                )
                if ref_type in ("view", "probe") else {}
            )
        return col_cache[ref]

    def tokens_for(ref: str) -> set[int]:
        if ref not in token_cache:
            ref_type = node_types.get(ref)
            token_cache[ref] = (
                read_string_tokens(
                    store, session_id, report.run_id, ref, ref_type
                )
                if ref_type in ("view", "probe") else set()
            )
        return token_cache[ref]

    claim_ids: set[str] = set()
    for section in report.sections:
        for claim in section.claims:
            tag = f"[{section.section_id}/{claim.claim_id}]"

            if claim.claim_id in claim_ids:
                violations.append(f"{tag} claim_id 重复。")
            claim_ids.add(claim.claim_id)

            # 2) 证据存在性 + 汇总引用证据的列数据/标签 token
            evidence_columns: dict[str, tuple[float, ...]] = {}
            evidence_tokens: set[int] = set()
            for ref in claim.evidence_view_ids:
                ref_columns = columns_for(ref)
                if not ref_columns and node_types.get(ref) not in ("view", "probe"):
                    violations.append(
                        f"{tag} 引用了不存在的证据：{ref}"
                    )
                for col, pool in ref_columns.items():
                    evidence_columns[col] = evidence_columns.get(col, ()) + pool
                evidence_tokens |= tokens_for(ref)
            if not any(evidence_columns.values()):
                violations.append(f"{tag} 没有任何可用证据数据。")
                continue

            # 3) 数字接地：普通数字先匹配度量列；未中则允许引用形态的维度标签 token
            plains, pcts = _extract_numbers(claim.text)
            for x in plains:
                if grounded(x, is_percent=False, columns=evidence_columns):
                    continue
                if label_token_ok(x, claim.text, evidence_tokens):
                    continue
                violations.append(
                    f"{tag} 数字 {x:g} 未在引用证据中找到来源（未接地）。"
                )
            for x in pcts:
                if not grounded(x, is_percent=True, columns=evidence_columns):
                    violations.append(
                        f"{tag} 百分比 {x:g}% 未在引用证据中找到来源（未接地）。"
                    )

            # 4) 因果护栏
            terms = find_causal_terms(claim.text)
            if terms:
                violations.append(
                    f"{tag} 包含因果性表述：{'、'.join(terms)}，"
                    "请改为相关/对比/假设类表述。"
                )
    return violations
