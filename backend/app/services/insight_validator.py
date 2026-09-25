"""Insight Validator：洞察门禁纯规则（FR-10），不调用 LLM。

四类检查：
1. 数字接地：文本中的每个数字必须能在引用的引擎结果中按展示精度找到；
2. 因果护栏：因果词表（中英）拦截；相关分析强制免责语；
3. 证据完整性：引用步骤/结果必须真实存在；
4. 统计方向一致：同比升降、相关正负与结果一致。
另含可信度封顶策略。
"""
from __future__ import annotations

import re

from app.schemas.common import Confidence
from app.schemas.insight import Insight, InsightSet
from app.services.insight_context import ResultDigest

# 因果词表（FR-10 明示）
CAUSAL_ZH = ["导致", "引起", "使得", "拉动", "提升了", "降低了", "驱动", "因果", "促使"]
CAUSAL_EN = [
    r"\bbecause\b", r"\bcause[sd]?\b", r"\bcausing\b",
    r"\bdrive[sd]?\b", r"\bdrove\b", r"\bdriven\b", r"\bdriving\b",
    r"\blead(?:s|ing)? to\b", r"\bled to\b", r"\bdue to\b",
]
# 合规豁免语境（先从文本中剔除再扫描）
EXEMPT_PHRASES = [
    "相关关系，不代表因果", "相关不代表因果", "不代表因果", "不构成因果关系",
    "correlation does not imply causation", "does not imply causation",
]
CORRELATION_DISCLAIMER = "相关关系，不代表因果"

# 抽取前剔除：免责语、日期/期间标签、百分比 token（百分比单独匹配）
_DATE_PATTERNS = [
    r"\d{4}年\d{1,2}月\d{1,2}日",
    r"\d{4}年\d{1,2}月",
    r"\d{4}年",
    r"\d{4}-\d{1,2}-\d{1,2}",
    r"\d{4}-\d{1,2}",
    r"\d{4}[ ]?[Qq]\d",
    r"\d{4}-W\d{1,2}",
    r"\d{4}/\d{1,2}(/\d{1,2})?",
]
_PCT_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?\s*%")
_NUM_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")

_UP_WORDS = ["上升", "增长", "增加", "提高", "走高", "上涨", "更高", "增多", "扩大"]
_DOWN_WORDS = ["下降", "减少", "降低", "走低", "下跌", "下滑", "更少", "缩小", "回落"]


def find_causal_terms(text: str) -> list[str]:
    """返回命中的因果词；合规免责语中的「因果」不计。"""
    cleaned = text
    for phrase in EXEMPT_PHRASES:
        cleaned = cleaned.replace(phrase, " ")
    hits = [w for w in CAUSAL_ZH if w in cleaned]
    low = cleaned.lower()
    for pat in CAUSAL_EN:
        if re.search(pat, low):
            hits.append(pat)
    return hits


def _extract_numbers(text: str) -> tuple[list[float], list[float]]:
    """返回（普通数字, 百分比数字）。日期标签与免责语不参与。"""
    s = text
    for phrase in EXEMPT_PHRASES:
        s = s.replace(phrase, " ")
    for pat in _DATE_PATTERNS:
        s = re.sub(pat, " ", s)
    # 编号型标识符（如 R59、Q1、SKU12）不作为业务数字
    s = re.sub(r"[A-Za-z]{1,3}\d+(?:\.\d+)?", " ", s)
    percents = []
    for m in _PCT_RE.findall(s):
        percents.append(round(float(m.replace("%", "").replace(",", "").strip()), 1))
    s = _PCT_RE.sub(" ", s)
    plains = []
    for m in _NUM_RE.findall(s):
        token = m.replace(",", "")
        if token in {"", "+", "-"}:
            continue
        plains.append(round(float(token), 2))
    return plains, percents


def _has_match(value: float, candidates: list[float], tol: float = 0.02) -> bool:
    return any(abs(value - c) <= tol for c in candidates)


def validate_insight(insight: Insight, digest: ResultDigest, index: int) -> list[str]:
    tag = f"第 {index + 1} 条洞察"
    errors: list[str] = []

    # 1) 因果护栏
    terms = find_causal_terms(insight.text)
    if terms:
        errors.append(f"{tag} 包含因果性表述：{'、'.join(terms)}，请改为相关/对比类客观表述。")
    if digest.terminal_op == "correlation" and CORRELATION_DISCLAIMER not in insight.text:
        errors.append(f"{tag} 属于相关分析结论，必须包含「{CORRELATION_DISCLAIMER}」提示。")

    # 2) 证据完整性：步骤必须真实存在
    step_ids = {s["step_id"] for s in digest.steps}
    for ref in insight.evidence_refs:
        if ref not in step_ids:
            errors.append(f"{tag} 引用了不存在的分析步骤：{ref}")

    # 3) 数字接地：声明的每个数字必须能在结果中定位
    plains_text, pcts_text = _extract_numbers(insight.text)
    declared_plain: list[float] = []
    declared_pct: list[float] = []
    for gn in insight.numbers:
        point = digest.find_point(gn.ref_step, gn.ref_kind, gn.ref_keys)
        if point is None:
            errors.append(
                f"{tag} 的数字 {gn.value:g}（{gn.label or gn.ref_step}）在引擎结果中找不到对应来源。"
            )
            continue
        if abs(round(gn.value, 2) - point.value) > 0.02:
            errors.append(
                f"{tag} 的数字 {gn.value:g} 与引擎结果值 {point.value:g} 不一致。"
            )
            continue
        if gn.ref_step not in insight.evidence_refs:
            errors.append(f"{tag} 的数字引用步骤 {gn.ref_step} 未出现在 evidence_refs 中。")
        (declared_pct if point.is_percent else declared_plain).append(point.value)

    # 文本数字必须有接地：普通数字在普通候选中，百分比在百分比候选中
    plain_pool = digest.plain_candidates()
    pct_pool = digest.pct_candidates()
    for x in plains_text:
        if not (_has_match(x, plain_pool) or _has_match(x, declared_plain)):
            errors.append(f"{tag} 中的数字 {x:g} 无法在引擎结果中找到来源（数字未接地）。")
    for x in pcts_text:
        if not (_has_match(x, pct_pool, tol=0.05) or _has_match(x, declared_pct, tol=0.05)):
            errors.append(f"{tag} 中的百分比 {x:g}% 无法在引擎结果中找到来源（数字未接地）。")
    # 声明数字也必须真的写进了文本
    for x in declared_plain:
        if not _has_match(x, plains_text):
            errors.append(f"{tag} 声明的数字 {x:g} 未在结论文本中出现。")
    for x in declared_pct:
        if not _has_match(x, pcts_text, tol=0.05):
            errors.append(f"{tag} 声明的百分比 {x:g}% 未在结论文本中出现。")

    # 4) 统计方向一致性
    for gn in insight.numbers:
        point = digest.find_point(gn.ref_step, gn.ref_kind, gn.ref_keys)
        if point is None:
            continue
        if point.keys.get("field") == "growth_pct":
            if point.value > 0 and not any(w in insight.text for w in _UP_WORDS):
                errors.append(f"{tag} 增长率为正但文本未表达上升方向，或方向表述可能不一致。")
            if point.value < 0 and not any(w in insight.text for w in _DOWN_WORDS):
                errors.append(f"{tag} 增长率为负但文本未表达下降方向，或方向表述可能不一致。")
        if point.keys.get("field") == "coefficient":
            if point.value > 0 and ("负相关" in insight.text or "负向" in insight.text):
                errors.append(f"{tag} 相关系数为正，文本却描述为负相关/负向。")
            if point.value < 0 and ("正相关" in insight.text or "正向" in insight.text):
                errors.append(f"{tag} 相关系数为负，文本却描述为正相关/正向。")
    return errors


def validate_insight_set(insight_set: InsightSet, digest: ResultDigest) -> list[str]:
    errors: list[str] = []
    for i, ins in enumerate(insight_set.insights):
        errors.extend(validate_insight(ins, digest, i))
    return errors


def apply_confidence_policy(insight: Insight, digest: ResultDigest) -> Insight:
    """服务端权威覆盖置信度：LLM 不能自定可信度。"""
    n_risks = len(digest.risks)
    if n_risks == 0:
        insight.confidence = Confidence.high
        insight.needs_further_validation = False
        insight.confidence_reason = "数字全部接地、证据完整，结果校验全部通过。"
    elif n_risks == 1:
        insight.confidence = Confidence.medium
        insight.needs_further_validation = True
        insight.confidence_reason = "存在 1 项风险因素：" + digest.risks[0] + "，建议进一步验证。"
    else:
        insight.confidence = Confidence.low
        insight.needs_further_validation = True
        insight.confidence_reason = (
            "存在多项风险因素：" + "；".join(digest.risks) + "，结论需进一步验证。"
        )
    if digest.terminal_op == "correlation":
        insight.disclaimer = CORRELATION_DISCLAIMER
    return insight
