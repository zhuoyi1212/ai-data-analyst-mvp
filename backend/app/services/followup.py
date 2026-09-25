"""Follow-up 追问生成（FR-11）：确定性后置校验同推荐器。"""
from __future__ import annotations

import json
from pathlib import Path

from app.schemas.dictionary import DataDictionary
from app.schemas.followup import FollowUpSet
from app.services.engine.catalog import TIME_OPS, known_ops
from app.services.insight_context import build_result_digest
from app.services.ledger_loader import load_execution_context
from app.services.llm import generate_json
from app.services.llm.errors import ContractError
from app.services.llm.fixtures import FixtureMissingError
from app.services.offline_fallback import fallback_followups
from app.services.storage import SessionStore, StorageError
from app.services.validator import ensure_consumable

_FEATURE_WORDS = [
    "趋势", "对比", "占比", "排名", "异常", "离群", "相关", "下钻", "拆解",
    "切换", "粒度", "总量", "均值", "增长", "同比", "环比", "分组", "维度",
]

_SYSTEM = (
    "你是后续分析问题推荐器。基于刚完成的分析方案、结果与数据字典，"
    "推荐 3-5 个用户最可能继续追问的问题（下钻某维度、切换时间粒度、"
    "查看相关指标、异常项拆解、对比其他成员等）。"
    "每条必须给出具体推荐依据（点名字段或本次结果特征），"
    "target_op 只能取 aggregate/group_by/share/top_n/time_series/period_compare/"
    "compare_groups/correlation/outlier_flag；没有日期字段时禁止时间类追问；"
    "plan_hint 可预填参数（如 dimension、granularity）。仅输出 JSON。"
)


def _build_user(dictionary: DataDictionary, digest_payload: dict) -> str:
    fields = ["可使用字段："]
    for f in dictionary.fields:
        if not f.ignored:
            fields.append(f"- 「{f.name}」（{f.semantic_type.value}）{f.meaning}")
    fields.append("刚完成的分析结果摘要：")
    fields.append(json.dumps(digest_payload, ensure_ascii=False))
    fields.append(
        '输出：{"questions":[{"text","rationale","fields":[...],"target_op","plan_hint"}]}'
    )
    return "\n".join(fields)


def validate_followups(followups: FollowUpSet, dictionary: DataDictionary) -> list[str]:
    errors: list[str] = []
    usable = {f.name for f in dictionary.fields if not f.ignored}
    date_cols = set(dictionary.date_fields())
    for i, q in enumerate(followups.questions):
        tag = f"第 {i + 1} 条追问「{q.text[:12]}」"
        if q.target_op not in known_ops():
            errors.append(f"{tag} 的目标算子不在算子目录：{q.target_op}")
        if not set(q.fields) <= usable:
            errors.append(f"{tag} 引用了不存在或已忽略的字段：{set(q.fields) - usable}")
        grounded = any(name in q.rationale for name in usable) or any(
            w in q.rationale for w in _FEATURE_WORDS)
        if not grounded:
            errors.append(f"{tag} 的推荐依据未引用任何字段名或结果特征。")
        if not date_cols and q.target_op in TIME_OPS:
            errors.append(f"{tag} 在没有日期字段时推荐了时间类追问。")
        if q.target_op in TIME_OPS and not any(c in date_cols for c in q.fields):
            errors.append(f"{tag} 的时间类追问未引用日期字段。")
    return errors


def generate_followups(
    session_id: str, store: SessionStore, *, fixture_name: str | None = None
) -> FollowUpSet:
    ensure_consumable(session_id, store)
    dictionary = DataDictionary.model_validate(store.read_artifact(session_id, "dictionary"))
    ledger, plan_artifact, validation, result = load_execution_context(session_id, store)
    digest = build_result_digest(ledger, plan_artifact, result, validation)

    meta = store.get_meta(session_id)
    base = fixture_name or Path(meta.get("source_sample") or meta["filename"]).stem
    try:
        followups = generate_json(
            stage="followup",
            fixture_name=f"{base}_followup",
            system=_SYSTEM,
            user=_build_user(dictionary, digest.llm_payload()),
            schema=FollowUpSet,
            business_validator=lambda obj: validate_followups(obj, dictionary),
        )
    except FixtureMissingError:
        # 离线规则降级：基于字段与当前结果确定性生成可下钻的追问
        try:
            df = store.load_snapshot(session_id)
        except StorageError:
            df = store.load_original(session_id)
        followups = fallback_followups(dictionary, digest, df)
    errors = validate_followups(followups, dictionary)
    if errors:  # 双保险
        raise ContractError("followup", errors)
    store.write_artifact(session_id, "followups",
                         followups.model_dump(mode="json"))
    store.update_meta(session_id, stage="followup")
    return followups
