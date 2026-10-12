"""开放式关键发现：模型综合已执行证据，数值与引用经确定性门禁。"""
from __future__ import annotations

import hashlib
import json
import math
import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.dashboard import DashboardArtifact, Finding
from app.services.insight_validator import _extract_numbers, find_causal_terms
from app.services.llm import generate_json
from app.services.llm.errors import LLMError
from app.services.llm.fixtures import FixtureMissingError

VERSION = 1
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "dashboard_findings.md"


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=500)
    type: str = Field(min_length=1, max_length=24)
    problem_key: str = Field(default="", max_length=64)
    importance: Literal["high", "medium", "low"]
    evidence_view_ids: list[str] = Field(min_length=1, max_length=8)


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[_Draft] = Field(default_factory=list, max_length=5)


def _bare_anomaly(refs, artifact: DashboardArtifact) -> bool:
    return bool(refs) and all(artifact.views.get(ref) and artifact.views[ref].type == "anomaly" for ref in refs)


def _catalog(artifact: DashboardArtifact) -> dict:
    evidence = []
    for card in artifact.views.values():
        if not card.consumable or not card.data:
            continue
        evidence.append({
            "view_id": card.view_id, "type": card.type, "title": card.title,
            "scope": card.scope_label, "metric": card.metric_label,
            "chart_spec": card.chart_spec.model_dump(mode="json") if card.chart_spec else None,
            "columns": [col.model_dump() for col in card.data.columns],
            "rows": card.data.rows if card.data.kind == "aggregate" else [],
            "interpretation": card.interpretation,
            "data_kind": card.data.kind,
            "page": card.data.page.model_dump() if card.data.page else None,
        })
    trusted = [f.model_dump(mode="json") for f in artifact.findings if artifact.findings_version == 0 and not _bare_anomaly(f.evidence_view_ids, artifact)]
    trusted.extend(c.model_dump(mode="json") for c in artifact.insights if not _bare_anomaly(c.evidence_view_ids, artifact))
    return {"title": artifact.title, "scope": artifact.scope.model_dump(mode="json"),
            "kpis": [k.model_dump(mode="json") for k in artifact.kpis],
            "evidence": evidence, "computed_observations": trusted}


def validate_findings(output: _Output, artifact: DashboardArtifact) -> list[str]:
    errors = []
    seen = set()
    problems = set()
    catalog = _catalog(artifact)
    for draft in output.findings:
        refs = set(draft.evidence_view_ids)
        if draft.title in seen:
            errors.append("重复的业务发现，请合并。")
        seen.add(draft.title)
        problem = draft.problem_key.strip().lower()
        if problem and problem in problems:
            errors.append(f"业务问题 {problem} 被拆成多条，请合并概览与下钻证据。")
        problems.add(problem)
        if draft.importance == "low":
            errors.append(f"「{draft.title}」重要性较低，应留在图表解读中，不进入关键发现；不要仅修改评分。")
        cards = [artifact.views.get(ref) for ref in refs]
        if any(card is None or not card.consumable or not card.data for card in cards):
            errors.append(f"「{draft.title}」引用了不存在或不可消费的证据。")
            continue
        if all(card.type == "anomaly" for card in cards):
            errors.append("单独的统计离群明细不足以证明业务异常，请使用可比基线与业务影响证据。")
        if all(card.type == "overview" for card in cards):
            errors.append("请不要把顶部 KPI 总量复述为关键发现。")
        text = draft.title + " " + draft.summary
        if re.search(r"[一二三四五六七八九十百]+分之[一二三四五六七八九十百]+|[一二三四五六七八九十百]+倍", text):
            errors.append(f"「{draft.title}」用了未计算的中文比例或倍数，请删去该比例，仅引用已计算的原值。")
        if any(word in text for word in ("转负", "转正")) and not any(card.type == "trend" for card in cards):
            errors.append(f"「{draft.title}」只有分组证据，不能声称转正或转负；请准确描述当前盈利或亏损。")
        if find_causal_terms(text):
            errors.append("不能将描述性证据解释为因果关系。")
        plains, percents = [], []
        for card in cards:
            spec = card.chart_spec
            if card.type == "ranking" and card.data.kind == "aggregate":
                plains.append(len(card.data.rows))
            for row in card.data.rows:
                for col in card.data.columns:
                    if col.dtype not in ("number", "integer") or (spec and col.name in (spec.x_field, spec.dimension)):
                        continue
                    if col.name.startswith("is_") or col.name.lower().endswith("id"):
                        continue
                    value = row.get(col.name)
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                        continue
                    plains.extend(round(value, digits) for digits in (0, 1, 2))
                    if col.name == "share" or card.type == "profitability":
                        percents.extend(round(value * 100, digits) for digits in (0, 1, 2))
        for fact in catalog["computed_observations"]:
            fact_refs = set(fact["evidence_view_ids"])
            if fact_refs and fact_refs <= refs:
                a, b = _extract_numbers(fact["title"] + " " + fact["summary"])
                plains.extend(a)
                percents.extend(b)
                if isinstance(fact.get("delta_pct"), (int, float)):
                    percents.append(round(fact["delta_pct"] * 100, 1))
        if all(card.type == "ranking" for card in cards) and any(word in text for word in ("集中", "依赖")):
            errors.append(f"「{draft.title}」只有头尾排名，无法证明整体集中度或依赖，请删去集中度判断或引用完整份额证据。")
        mentioned, percentages = _extract_numbers(text)
        for value in mentioned:
            if not any(abs(value - candidate) <= .011 for candidate in plains):
                sources = []
                for source in artifact.views.values():
                    if not source.consumable or not source.data:
                        continue
                    for row in source.data.rows:
                        raw = row.get("value")
                        if isinstance(raw, (int, float)) and any(abs(value - round(abs(raw), digit)) <= .011 for digit in (0, 1, 2)):
                            sources.append(f"{source.view_id}（范围：{source.scope_label}，原值：{raw:g}）")
                            break
                hint = "；可能的来源：" + "、".join(sources[:4]) if sources else ""
                errors.append(f"「{draft.title}」中的数值 {value:g} 不在引用证据中。补充该数值的真实 view_id 或删掉；负利润须保留负号，不改成正数亏损额{hint}。")
        for value in percentages:
            if not any(abs(value - candidate) <= .051 for candidate in percents):
                errors.append(f"「{draft.title}」中的百分比 {value:g}% 不在引用的已计算证据中。")
    return errors


def _fallback(artifact: DashboardArtifact) -> list[Finding]:
    # 离线 / 模型不可用：保留既有的确定性发现（去掉低重要性条目），不把计算候选提升为
    # 关键发现——提升会把统计噪声变成业务结论（如均匀数据被判「高度集中」）。
    # 确定性异常发现（_detect_anomaly）是产品要求的合法产出，按 T12 契约予以保留。
    out = [finding for finding in artifact.findings if finding.importance != "low"]
    return [finding.model_copy(update={"finding_id": f"finding_{i:02d}"}) for i, finding in enumerate(out[:5], 1)]


def review_key_findings(artifact: DashboardArtifact) -> DashboardArtifact:
    if artifact.state == "empty":
        return artifact.model_copy(update={"findings": [], "risks": [], "findings_version": VERSION, "findings_source": "computed"})
    catalog = _catalog(artifact)
    prompt = PROMPT_PATH.read_text()
    fingerprint = hashlib.sha256((prompt + json.dumps(catalog, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()[:16]
    def validate_for_repair(result: _Output) -> list[str]:
        errors = validate_findings(result, artifact)
        if errors:
            errors.append("逐条修正以上错误。重复问题合并正文及引用；低重要性条目直接删除。允许减少条数，不要补齐。")
        return errors
    try:
        output = generate_json(stage="key_findings", fixture_name=f"{artifact.run_id}_{fingerprint}",
                               system=prompt, user=json.dumps(catalog, ensure_ascii=False),
                               schema=_Output, max_repair=2,
                               business_validator=validate_for_repair)
        findings = [Finding(finding_id=f"finding_{i:02d}", **draft.model_dump(exclude={"problem_key"})) for i, draft in enumerate(output.findings, 1)]
        source, notice = "model", ""
    except FixtureMissingError:
        findings, source, notice = _fallback(artifact), "computed", ""
    except LLMError as exc:
        logging.getLogger(__name__).warning("Key findings review failed: %s", exc)
        findings, source, notice = _fallback(artifact), "computed", "模型暂未生成通过校验的发现，当前显示已验证的计算结论。"
    return artifact.model_copy(update={"findings": findings,
        "risks": [f for f in findings if f.type == "risk"], "findings_version": VERSION,
        "findings_source": source, "findings_notice": notice})
