"""Semantic Profiler 服务：规则 + LLM 补全合并、确认闸门。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.schemas.common import Confidence, SemanticType
from app.schemas.dictionary import (
    DataDictionary,
    FieldProfile,
    LLMProfileResult,
)
from app.services.llm import generate_json
from app.services.llm.fixtures import FixtureMissingError
from app.services.profiler_rules import RuleProfile, detect_relations, rule_profile_all
from app.services.storage import SessionStore

_SYSTEM = (
    "你是数据语义分析器。根据给定的字段名、样例值与统计特征，判断每个字段的业务语义。"
    "只能输出 JSON，字段必须与输入一一对应。"
    "semantic_type 取值：dimension(分类维度)/metric(可计算数值指标)/date(日期)/"
    "id(唯一标识)/geo(地区)/unknown(无法确定)。"
    "candidates 给出 2-3 个候选业务含义；confidence 仅在证据充分时给 high，"
    "取值混乱或语义不明时给 medium 或 low。"
)


def _build_prompt(df_rows: int, rules: dict[str, RuleProfile]) -> str:
    lines = [f"数据共 {df_rows} 行，字段如下："]
    for name, rp in rules.items():
        parts = [f"字段名「{name}」", f"物理类型={rp.physical_type}"]
        if rp.stats:
            parts.append(f"统计={rp.stats}")
        parts.append(f"空值率={rp.null_rate}")
        parts.append(f"基数={rp.cardinality}")
        parts.append(f"样例={rp.examples}")
        if rp.hints:
            parts.append(f"规则线索={rp.hints}")
        lines.append("- " + "，".join(parts))
    lines.append(
        '输出格式：{"fields":[{"name","semantic_type","meaning","unit":'
        '"单位或null","candidates":[...],"confidence"}]}'
    )
    return "\n".join(lines)


def _fallback_meaning(rp: RuleProfile) -> str:
    st = rp.locked or rp.guess
    return {
        SemanticType.date: "日期/时间字段",
        SemanticType.id: "业务唯一标识字段",
        SemanticType.metric: f"数值指标「{rp.name}」",
        SemanticType.dimension: f"分类维度「{rp.name}」",
        SemanticType.geo: f"地理维度「{rp.name}」",
        SemanticType.unknown: f"字段「{rp.name}」含义待确认",
    }.get(st, f"字段「{rp.name}」")


def _merge(
    rules: dict[str, RuleProfile], llm_fields: dict[str, Any]
) -> list[FieldProfile]:
    profiles: list[FieldProfile] = []
    for name, rp in rules.items():
        enriched = llm_fields.get(name)
        locked = rp.locked

        if locked is not None:
            semantic = locked
            confidence = Confidence.high
            meaning = enriched.meaning if enriched else _fallback_meaning(rp)
            unit = enriched.unit if enriched else None
            candidates = []
        elif enriched is not None:
            semantic = enriched.semantic_type
            meaning = enriched.meaning
            unit = enriched.unit
            candidates = enriched.candidates
            confidence = enriched.confidence
            # 护栏：高基数自由文本即使模型说 high，也降为 medium 要求用户确认
            if (
                rp.physical_type == "string"
                and rp.cardinality > 30
                and semantic in {SemanticType.dimension, SemanticType.unknown}
                and confidence == Confidence.high
            ):
                confidence = Confidence.medium
            # 数值列不允许被标成日期/标识
            if rp.physical_type in {"integer", "float"} and semantic in {
                SemanticType.date,
                SemanticType.geo,
            }:
                semantic = rp.guess or SemanticType.metric
                confidence = Confidence.medium
        else:
            semantic = rp.guess or SemanticType.unknown
            meaning = _fallback_meaning(rp)
            unit = None
            candidates = []
            confidence = Confidence.low if semantic == SemanticType.unknown else Confidence.medium

        confirmed = confidence == Confidence.high and semantic != SemanticType.unknown
        profiles.append(
            FieldProfile(
                name=name,
                physical_type=rp.physical_type,
                semantic_type=semantic,
                meaning=meaning,
                unit=unit,
                candidates=candidates,
                confidence=confidence,
                examples=rp.examples,
                is_date=semantic == SemanticType.date,
                is_metric=semantic == SemanticType.metric,
                null_rate=rp.null_rate,
                cardinality=rp.cardinality,
                stats=rp.stats,
                rule_hints=rp.hints,
                confirmed=confirmed,
            )
        )
    return profiles


def is_complete(dictionary: DataDictionary) -> bool:
    for f in dictionary.fields:
        if f.ignored:
            continue
        if f.confirmed_by_user:
            continue
        if f.confidence == Confidence.high and f.semantic_type != SemanticType.unknown:
            continue
        return False
    return True


def _promote_rule_confident(
    profiles: list[FieldProfile], rules: dict[str, RuleProfile]
) -> list[FieldProfile]:
    """纯规则降级路径：规则证据明确的字段直接判为高置信，无需用户逐个确认。

    仍保持待确认的只剩语义 unknown（典型：高基数自由文本）。
    """
    for fp in profiles:
        if fp.confidence == Confidence.high or fp.semantic_type == SemanticType.unknown:
            continue
        rp = rules.get(fp.name)
        if rp is None:
            continue
        # 有规则线索；或数值列默认指标候选（物理类型已足够确定）
        if rp.hints or fp.physical_type in {"integer", "float"}:
            fp.confidence = Confidence.high
            fp.confirmed = True
    return profiles


def generate_dictionary(
    session_id: str,
    store: SessionStore,
    fixture_name: str | None = None,
) -> DataDictionary:
    meta = store.get_meta(session_id)
    fixture_name = (
        fixture_name
        or Path(meta.get("source_sample") or meta["filename"]).stem
    )
    df = store.load_original(session_id)
    rules = rule_profile_all(df)

    try:
        result = generate_json(
            stage="profile",
            fixture_name=fixture_name,
            system=_SYSTEM,
            user=_build_prompt(len(df), rules),
            schema=LLMProfileResult,
        )
        llm_fields = {item.name: item for item in result.fields}
    except FixtureMissingError:
        # 无固件且无 LLM key：纯规则降级，LLM 补全为空，全部字段走规则口径
        llm_fields = {}

    profiles = _merge(rules, llm_fields)
    if not llm_fields:
        profiles = _promote_rule_confident(profiles, rules)
    metric_cols = [f.name for f in profiles if f.semantic_type == SemanticType.metric]
    relations = detect_relations(df, metric_cols)

    dictionary = DataDictionary(
        session_id=session_id,
        fields=profiles,
        relations=relations,
        complete=is_complete(DataDictionary(session_id=session_id, fields=profiles)),
    )
    store.write_artifact(session_id, "dictionary", dictionary.model_dump(mode="json"))
    store.update_meta(session_id, stage="profile")
    return dictionary


def confirm_fields(
    session_id: str, decisions: list[dict[str, Any]], store: SessionStore
) -> DataDictionary:
    dictionary = DataDictionary.model_validate(
        store.read_artifact(session_id, "dictionary")
    )
    by_name = {d["name"]: d for d in decisions}
    for field in dictionary.fields:
        d = by_name.get(field.name)
        if d is None:
            continue
        field.ignored = bool(d.get("ignored", field.ignored))
        if "semantic_type" in d and d["semantic_type"]:
            field.semantic_type = SemanticType(d["semantic_type"])
        if d.get("meaning"):
            field.meaning = d["meaning"]
        if "unit" in d:
            field.unit = d.get("unit")
        field.confirmed_by_user = True
        field.confidence = Confidence.high
        field.confirmed = True
        field.is_date = field.semantic_type == SemanticType.date
        field.is_metric = field.semantic_type == SemanticType.metric

    dictionary.complete = is_complete(dictionary)
    store.write_artifact(session_id, "dictionary", dictionary.model_dump(mode="json"))
    return dictionary
