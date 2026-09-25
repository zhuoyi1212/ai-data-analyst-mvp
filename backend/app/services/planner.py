"""Analysis Planner：把问题翻译为有限算子方案（FR-5）。

LLM 只负责「选型与参数草拟」，服务端做两层确定性把关：
1. AnalysisPlan 判别联合（算子白名单、参数结构，禁自由代码）；
2. 对照数据字典做语义校验（字段存在/语义匹配/日期约束/依赖合法/末端产出）。
业务校验失败同样进入 ≤2 次带反馈修复；用户编辑后的方案必须重新过同一套校验。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.schemas.dictionary import DataDictionary
from app.schemas.plan import AnalysisPlan, PlanArtifact
from app.services.engine.catalog import (
    OP_META,
    terminal_chart,
    validate_plan_against_fields,
)
from app.services.llm import generate_json
from app.services.llm.errors import ContractError
from app.services.llm.fixtures import FixtureMissingError, question_variant
from app.services.offline_fallback import plan_for_question
from app.services.storage import SessionStore, StorageError

_SYSTEM = (
    "你是分析方案规划器，服务于不会写 SQL 的业务用户。"
    "把用户问题翻译为一个 AnalysisPlan JSON。硬性要求：\n"
    "1. steps 只能使用这 10 个算子：filter、aggregate、group_by、share、top_n、"
    "time_series、period_compare、compare_groups、correlation、outlier_flag，"
    "严禁输出代码、公式字符串或算子以外的任何操作；\n"
    "2. 只能引用我给出的字段名；时间算子必须使用已确认的日期字段；"
    "分组/占比/排名/对比的 dimension 必须是维度字段；指标参数必须是指标字段；\n"
    "3. 最后一步必须是产出结果的末端算子（filter 只能作为前置筛选）；\n"
    "4. 每个步骤写清 step_id、description（人读计算口径），depends_on 只能引用更早的步骤；\n"
    "5. 方案只需回答当前问题，步骤尽量少。仅输出 JSON。"
)

_OP_GUIDE = (
    "算子参数速查：filter{column,operator(==,!=,>,>=,<,<=,in,not_in,contains,between,"
    "date_between),value}；aggregate{column,func(sum,mean,median,count,count_distinct,"
    "min,max)}；group_by{dimension,metric,func,order,limit}；share{dimension,metric,func}；"
    "top_n{dimension,metric,func,n,order}；time_series{date_column,granularity"
    "(day,week,month,quarter,year),metric,func}；period_compare{date_column,period"
    "(yoy,mom,wow),metric,func}；compare_groups{dimension,members[≥2],metric,func}；"
    "correlation{column_x,column_y,method(pearson,spearman)}；"
    "outlier_flag{column,method(iqr,zscore),threshold}。"
)


def plan_hash(plan: AnalysisPlan) -> str:
    body = json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _build_user(dictionary: DataDictionary, question: str) -> str:
    lines = ["可使用的字段："]
    for f in dictionary.fields:
        if f.ignored:
            continue
        extra = []
        if f.meaning:
            extra.append(f.meaning)
        if f.unit:
            extra.append(f"单位 {f.unit}")
        if f.stats:
            stats = {k: v for k, v in f.stats.items() if k in {"enum_values", "min", "max"}}
            if stats:
                extra.append(f"特征 {json.dumps(stats, ensure_ascii=False)}")
        lines.append(
            f"- 「{f.name}」语义类型={f.semantic_type.value}"
            + (f"（{'，'.join(extra)}）" if extra else "")
        )
    lines.append(_OP_GUIDE)
    lines.append(f"用户问题：{question}")
    lines.append(
        '输出 JSON：{"question","data_scope","steps":[{"step_id","op","params",'
        '"description","depends_on"}],"expected_shape","chart_hint"}'
    )
    return "\n".join(lines)


def _business_errors(plan: AnalysisPlan, dictionary: DataDictionary) -> list[str]:
    errors = validate_plan_against_fields(plan, dictionary.usable_field_infos())
    ids = [s.step_id for s in plan.steps]
    seen: set[str] = set()
    for step in plan.steps:
        for dep in step.depends_on:
            if dep not in ids:
                errors.append(f"步骤 {step.step_id} 依赖了不存在的步骤：{dep}")
            elif ids.index(dep) >= ids.index(step.step_id):
                errors.append(f"步骤 {step.step_id} 只能依赖它之前的步骤：{dep}")
        seen.add(step.step_id)
    last = plan.steps[-1]
    if not OP_META[last.op]["terminal"]:
        errors.append("方案最后一步必须是产出结果的算子，不能以 filter 结束。")
    return errors


def _load_dictionary(session_id: str, store: SessionStore) -> DataDictionary:
    dictionary = DataDictionary.model_validate(store.read_artifact(session_id, "dictionary"))
    if not dictionary.complete:
        raise ValueError("请先完成字段语义确认。")
    return dictionary


def _draft_artifact(
    plan: AnalysisPlan, *, question: str, source_index: int | None
) -> PlanArtifact:
    return PlanArtifact(
        question=question,
        plan=plan,
        chart=terminal_chart(plan),
        locked=False,
        plan_hash=None,
        source_question_index=source_index,
    )


def generate_plan(
    session_id: str,
    question: str,
    store: SessionStore,
    *,
    fixture_name: str | None = None,
    source_question_index: int | None = None,
) -> PlanArtifact:
    dictionary = _load_dictionary(session_id, store)
    meta = store.get_meta(session_id)
    base = fixture_name or Path(meta.get("source_sample") or meta["filename"]).stem
    base_name = f"{base}_plan"
    # 多轮追问：优先使用与当前问题绑定的变体固件，缺失则回退到 canonical 固件。
    names = [question_variant(base_name, "plan", question), base_name]

    try:
        plan = generate_json(
            stage="plan",
            fixture_name=names,
            system=_SYSTEM,
            user=_build_user(dictionary, question),
            schema=AnalysisPlan,
            business_validator=lambda p: _business_errors(p, dictionary),
        )
    except FixtureMissingError:
        # 离线规则降级：按已推荐/追问问题或关键词，从有限算子集合确定性构造方案
        try:
            df = store.load_snapshot(session_id)
        except StorageError:
            df = store.load_original(session_id)
        plan = plan_for_question(question, dictionary, store, session_id, df)
        errors = _business_errors(plan, dictionary)
        if errors:
            raise ContractError("plan", errors)
    artifact = _draft_artifact(plan, question=question, source_index=source_question_index)
    store.write_artifact(session_id, "plan", artifact.model_dump(mode="json"))
    return artifact


def validate_edited_plan(raw: dict[str, Any], dictionary: DataDictionary) -> AnalysisPlan:
    """用户编辑后的方案重新过两层校验；不合法直接抛 ContractError。"""
    plan = AnalysisPlan.model_validate(raw)
    errors = _business_errors(plan, dictionary)
    if errors:
        raise ContractError("plan", errors)
    return plan


def edit_plan(session_id: str, raw: dict[str, Any], store: SessionStore) -> PlanArtifact:
    dictionary = _load_dictionary(session_id, store)
    plan = validate_edited_plan(raw, dictionary)
    existing = store.read_artifact(session_id, "plan") if store.has_artifact(session_id, "plan") else None
    artifact = _draft_artifact(
        plan,
        question=plan.question,
        source_index=existing.get("source_question_index") if existing else None,
    )
    store.write_artifact(session_id, "plan", artifact.model_dump(mode="json"))
    return artifact


def confirm_plan(
    session_id: str, store: SessionStore, raw: dict[str, Any] | None = None
) -> PlanArtifact:
    dictionary = _load_dictionary(session_id, store)
    if raw is not None:
        plan = validate_edited_plan(raw, dictionary)
        source_index = None
    else:
        if not store.has_artifact(session_id, "plan"):
            raise ValueError("尚无分析方案，请先生成或提交方案。")
        current = store.read_artifact(session_id, "plan")
        plan = validate_edited_plan(current["plan"], dictionary)
        source_index = current.get("source_question_index")
    artifact = PlanArtifact(
        question=plan.question,
        plan=plan,
        chart=terminal_chart(plan),
        locked=True,
        plan_hash=plan_hash(plan),
        source_question_index=source_index,
    )
    store.write_artifact(session_id, "plan", artifact.model_dump(mode="json"))
    store.update_meta(session_id, stage="plan", plan_hash=artifact.plan_hash)
    return artifact
