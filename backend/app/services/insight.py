"""Insight Generator：基于引擎结果生成结构化洞察（FR-9）。

LLM 只接收方案口径 + 聚合结果摘要 + 校验状态（无全量明细）；
输出立即经 insight_validator 门禁（数字接地/因果护栏/证据/方向），
失败进入 ≤2 次带反馈修复；置信度由服务端策略权威覆盖。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.schemas.insight import InsightSet
from app.services.insight_context import MAX_DIGEST_ROWS, build_result_digest
from app.services.insight_validator import apply_confidence_policy, validate_insight_set
from app.services.ledger_loader import load_execution_context
from app.services.llm import generate_json
from app.services.llm.errors import ContractError
from app.services.llm.fixtures import FixtureMissingError, question_variant
from app.services.offline_fallback import fallback_insights
from app.services.storage import SessionStore
from app.services.validator import ensure_consumable

_SYSTEM = (
    "你是业务洞察生成器，为不会写 SQL 的业务用户服务。"
    "只能基于给定的引擎计算结果摘要撰写 2-5 条简洁专业的洞察。\n"
    "铁律：\n"
    "1. 只能使用摘要中出现的数字，并按其展示精度逐字引用，严禁心算或编造任何新数字；"
    "日期标签（如 2024-03）原样书写；\n"
    "2. 严禁因果性词汇：导致/引起/使得/拉动/提升了/驱动/因果/because/cause/drive/lead to/due to；"
    "改用「与…相关、占比更高、同期相比变化、伴随出现」等表述；\n"
    "3. 每条洞察 evidence_refs 必须使用 steps 中真实存在的 step_id；"
    "numbers 中每个数字都要给出 ref_step/ref_kind/ref_keys：指标卡用 kind=metric、keys={}；"
    "分组/占比/时间行用 kind=row，keys 照抄 result_rows 的键；"
    "增长率/相关系数等用 kind=summary，keys 照抄 result_summary 字段名；\n"
    "4. 相关分析（terminal_op=correlation）的每条洞察必须以"
    "「（相关关系，不代表因果）」结尾；\n"
    "5. 增长为正才可说上升/增长，为负必须说下降/减少；宁缺毋滥。仅输出 JSON。"
)


def _build_user(digest_payload: dict) -> str:
    return (
        "以下是本次分析的引擎结果摘要（数字均已按展示精度处理）：\n"
        + json.dumps(digest_payload, ensure_ascii=False, indent=2)
        + '\n输出：{"insights":[{"text","type","evidence_refs":[...],'
        '"numbers":[{"value","label","ref_step","ref_kind","ref_keys"}],'
        '"confidence","confidence_reason","needs_further_validation","disclaimer"}]}；'
        "type 取值 key_finding/trend/comparison/anomaly/risk。"
    )


def generate_insights(
    session_id: str, store: SessionStore, *, fixture_name: str | None = None
) -> InsightSet:
    ensure_consumable(session_id, store)  # fail 阻断 / warn 须知悉
    ledger, plan_artifact, validation, result = load_execution_context(session_id, store)
    digest = build_result_digest(ledger, plan_artifact, result, validation)

    payload = digest.llm_payload()
    meta = store.get_meta(session_id)
    base = fixture_name or Path(meta.get("source_sample") or meta["filename"]).stem
    base_name = f"{base}_insights"
    # 多轮追问：按当前已执行方案的问题选择对应洞察固件，缺失则回退 canonical。
    names = [question_variant(base_name, "insights", digest.question), base_name]

    try:
        insight_set = generate_json(
            stage="insights",
            fixture_name=names,
            system=_SYSTEM,
            user=_build_user(payload),
            schema=InsightSet,
            business_validator=lambda obj: validate_insight_set(obj, digest),
        )
    except FixtureMissingError:
        # 离线规则降级：模板化组织引擎结果数字，仍强制通过同一套接地/因果门禁
        insight_set = fallback_insights(digest)
        errors = validate_insight_set(insight_set, digest)
        if errors:
            raise ContractError("insights", errors)
    for ins in insight_set.insights:
        apply_confidence_policy(ins, digest)

    store.write_artifact(session_id, "insights", {
        "insights": [i.model_dump(mode="json") for i in insight_set.insights],
        "result_digest": payload,
        "digest_row_cap": MAX_DIGEST_ROWS,
    })
    store.update_meta(session_id, stage="insight")
    return insight_set
