"""Question Recommender：LLM 生成 + 确定性后置规则（FR-4）。

确定性规则负责：数量、分类覆盖、依据接地（字段名/特征词）、算子合法、
无日期字段时禁止时间类问题、分类与算子一致性。
"""
from __future__ import annotations

from pathlib import Path

from app.schemas.common import QuestionCategory, SemanticType
from app.schemas.dictionary import DataDictionary
from app.schemas.question import QuestionSet, RecommendedQuestion
from app.services.engine.catalog import TIME_OPS, known_ops
from app.services.llm import generate_json
from app.services.llm.errors import ContractError
from app.services.llm.fixtures import FixtureMissingError
from app.services.offline_fallback import fallback_questions
from app.services.storage import SessionStore, StorageError

_FEATURE_WORDS = [
    "趋势", "对比", "占比", "排名", "异常", "离群", "相关", "总量", "汇总",
    "均值", "增长", "同比", "环比", "缺失", "高基数", "时间", "枚举",
]

_CATEGORY_OPS: dict[QuestionCategory, set[str]] = {
    QuestionCategory.overview: {"aggregate"},
    QuestionCategory.trend: {"time_series", "period_compare"},
    QuestionCategory.comparison: {"compare_groups", "group_by", "period_compare"},
    QuestionCategory.share: {"share"},
    QuestionCategory.ranking: {"top_n"},
    QuestionCategory.anomaly: {"outlier_flag"},
    QuestionCategory.correlation: {"correlation"},
}

_SYSTEM = (
    "你是业务分析问题推荐器。基于数据字典中的字段含义、字段关系与数据特征，"
    "为不会写 SQL 的业务用户推荐 6-10 个可以直接分析的具体问题。"
    "要求：覆盖至少 4 个分类；每条必须说明推荐依据（点名相关字段或数据特征）；"
    "target_op 只能取 aggregate/group_by/share/top_n/time_series/period_compare/"
    "compare_groups/correlation/outlier_flag；没有日期字段时禁止趋势类问题。"
    "仅输出 JSON。"
)


def _build_user(dictionary: DataDictionary) -> str:
    lines = ["数据字典："]
    for f in dictionary.fields:
        if f.ignored:
            continue
        line = f"- 「{f.name}」({f.semantic_type.value})：{f.meaning}"
        if f.stats:
            line += f"，统计特征 {f.stats}"
        if f.unit:
            line += f"，单位 {f.unit}"
        lines.append(line)
    if dictionary.relations:
        lines.append("字段关系：")
        for r in dictionary.relations:
            lines.append(f"- {r.note}")
    has_date = bool(dictionary.date_fields())
    lines.append(f"是否存在日期字段：{'是（' + '、'.join(dictionary.date_fields()) + '）' if has_date else '否'}")
    lines.append(
        '输出：{"questions":[{"text","category","rationale","fields":[...],'
        '"target_op","confidence"}]}；category 取值 overview/trend/comparison/'
        "share/ranking/anomaly/correlation。"
    )
    return "\n".join(lines)


def validate_question_set(qs: QuestionSet, dictionary: DataDictionary) -> list[str]:
    errors: list[str] = []
    usable = {f.name for f in dictionary.fields if not f.ignored}
    date_cols = set(dictionary.date_fields())

    if not (6 <= len(qs.questions) <= 10):
        errors.append(f"推荐问题数量应为 6-10 个，实际 {len(qs.questions)} 个。")
    cats = {q.category for q in qs.questions}
    if len(cats) < 4:
        errors.append(f"推荐问题至少覆盖 4 个分类，实际覆盖 {len(cats)} 个。")

    for i, q in enumerate(qs.questions):
        tag = f"第 {i + 1} 条问题「{q.text[:12]}」"
        if q.target_op not in known_ops():
            errors.append(f"{tag} 的目标算子不在有限算子目录：{q.target_op}")
        if not set(q.fields) <= usable:
            errors.append(f"{tag} 引用了不存在或已忽略的字段：{set(q.fields) - usable}")
        grounded = any(name in q.rationale for name in usable) or any(
            w in q.rationale for w in _FEATURE_WORDS
        )
        if not grounded:
            errors.append(f"{tag} 的推荐依据未引用任何字段名或数据特征。")
        if q.target_op not in _CATEGORY_OPS.get(q.category, set()):
            errors.append(f"{tag} 的分类 {q.category.value} 与算子 {q.target_op} 不匹配。")
        if not date_cols and (
            q.category == QuestionCategory.trend or q.target_op in TIME_OPS
        ):
            errors.append(f"{tag} 在没有日期字段时推荐了时间趋势类问题。")
        if q.target_op in TIME_OPS and not any(c in date_cols for c in q.fields):
            errors.append(f"{tag} 的时间类问题未引用日期字段。")
    return errors


def recommend(
    session_id: str,
    store: SessionStore,
    variant: int = 0,
    fixture_name: str | None = None,
) -> QuestionSet:
    dictionary = DataDictionary.model_validate(store.read_artifact(session_id, "dictionary"))
    if not dictionary.complete:
        raise ValueError("请先完成字段语义确认。")
    # 质量快照非强制（允许用户保留全部问题），但推荐以处理后的字典为准
    meta = store.get_meta(session_id)
    base = fixture_name or Path(meta.get("source_sample") or meta["filename"]).stem
    name = f"{base}_questions" if variant == 0 else f"{base}_questions_v{variant}"

    qs = None
    try:
        qs = generate_json(
            stage="questions",
            fixture_name=name,
            system=_SYSTEM,
            user=_build_user(dictionary),
            schema=QuestionSet,
        )
        errors = validate_question_set(qs, dictionary)
        if errors:
            raise ContractError("questions", errors)
    except FixtureMissingError:
        # 离线规则降级：基于字段类型确定性生成（尽量满足分类覆盖，不强行阻断）
        try:
            df = store.load_snapshot(session_id)
        except StorageError:
            df = store.load_original(session_id)
        qs = fallback_questions(dictionary, df)
    store.write_artifact(session_id, "questions", qs.model_dump(mode="json"))
    store.update_meta(session_id, stage="questions")
    return qs
