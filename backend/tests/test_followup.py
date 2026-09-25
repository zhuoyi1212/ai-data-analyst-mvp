"""TR-15.1：追问数量/依据接地/无日期限制/可回流规划器。"""
from __future__ import annotations

import pandas as pd
import pytest

from app.schemas.common import AggFunc, ChartType, Confidence, SemanticType
from app.schemas.dictionary import DataDictionary, FieldProfile
from app.schemas.followup import FollowUpSet
from app.schemas.plan import (
    AnalysisPlan,
    GroupByParams,
    GroupByStep,
    PlanArtifact,
    ShareParams,
    ShareStep,
)
from app.services.executor import execute
from app.services.followup import generate_followups
from app.services.llm.errors import ContractError
from app.services.planner import generate_plan
from app.services.planner import plan_hash
from app.services.storage import SessionStore
from app.services.validator import run_validation

_TYPE = {"日期": SemanticType.date, "区域": SemanticType.dimension,
         "品类": SemanticType.dimension, "金额": SemanticType.metric}


def _pipeline(tmp_path, df, plan, chart):
    store = SessionStore(tmp_path / "storage")
    sid = store.create_from_bytes("d.csv", df.to_csv(index=False).encode())["session_id"]
    dictionary = DataDictionary(session_id=sid, complete=True, fields=[
        FieldProfile(name=c, physical_type="string", semantic_type=_TYPE[c],
                     confidence=Confidence.high, confirmed=True, confirmed_by_user=True)
        for c in df.columns])
    store.write_artifact(sid, "dictionary", dictionary.model_dump(mode="json"))
    h = store.save_snapshot(sid, df)
    store.write_artifact(sid, "quality",
                         {"complete": True, "snapshot_hash": h, "snapshot_rows": len(df),
                          "issues": [], "decisions": {}})
    store.write_artifact(sid, "plan", PlanArtifact(
        question=plan.question, plan=plan, chart=chart, locked=True,
        plan_hash=plan_hash(plan)).model_dump(mode="json"))
    execute(sid, store)
    run_validation(sid, store)
    return store, sid, dictionary


_VALID = {"questions": [
    {"text": "按品类下钻销售额", "rationale": "已有「品类」维度，可对「金额」分组下钻",
     "fields": ["品类", "金额"], "target_op": "group_by",
     "plan_hint": {"dimension": "品类"}},
    {"text": "各区域占比如何", "rationale": "基于「区域」维度可拆解「金额」占比结构",
     "fields": ["区域", "金额"], "target_op": "share", "plan_hint": {"dimension": "区域"}},
    {"text": "Top3 品类是谁", "rationale": "按「品类」对「金额」聚合即可排名",
     "fields": ["品类", "金额"], "target_op": "top_n", "plan_hint": {"n": 3}},
]}


def test_valid_followups(tmp_path, llm_env):
    df = pd.DataFrame({"日期": pd.to_datetime(["2024-01-01", "2024-02-01"]),
                       "区域": ["East", "West"], "品类": ["A", "B"],
                       "金额": [100.0, 200.0]})
    plan = AnalysisPlan(question="区域销售额", steps=[
        GroupByStep(step_id="g1", op="group_by",
                    params=GroupByParams(dimension="区域", metric="金额", func=AggFunc.sum))])
    store, sid, _ = _pipeline(tmp_path, df, plan, ChartType.bar)
    llm_env("followup", "mini_followup", _VALID)
    followups = generate_followups(sid, store, fixture_name="mini")
    assert len(followups.questions) == 3
    assert store.get_meta(sid)["stage"] == "followup"


def test_no_date_rejects_time_followup(tmp_path, llm_env):
    df = pd.DataFrame({"区域": ["East", "West"], "金额": [100.0, 200.0]})
    plan = AnalysisPlan(question="区域占比", steps=[
        ShareStep(step_id="s1", op="share",
                  params=ShareParams(dimension="区域", metric="金额", func=AggFunc.sum))])
    store, sid, _ = _pipeline(tmp_path, df, plan, ChartType.share_bar)
    bad = {"questions": [
        {"text": "按月趋势", "rationale": "观察「金额」随时间的趋势变化",
         "fields": ["金额"], "target_op": "time_series"},
        {"text": "区域排名", "rationale": "按「区域」聚合「金额」排名",
         "fields": ["区域", "金额"], "target_op": "top_n", "plan_hint": {"n": 3}},
        {"text": "区域对比", "rationale": "「区域」分组对比「金额」均值",
         "fields": ["区域", "金额"], "target_op": "compare_groups",
         "plan_hint": {"members": ["East", "West"]}},
    ]}
    llm_env("followup", "mini_followup", bad)
    with pytest.raises(ContractError) as exc:
        generate_followups(sid, store, fixture_name="mini")
    assert any("时间" in r for r in exc.value.reasons)


def test_ungrounded_rationale_rejected(tmp_path, llm_env):
    df = pd.DataFrame({"日期": pd.to_datetime(["2024-01-01", "2024-02-01"]),
                       "区域": ["East", "West"], "金额": [100.0, 200.0]})
    plan = AnalysisPlan(question="区域销售额", steps=[
        GroupByStep(step_id="g1", op="group_by",
                    params=GroupByParams(dimension="区域", metric="金额", func=AggFunc.sum))])
    store, sid, _ = _pipeline(tmp_path, df, plan, ChartType.bar)
    bad = {"questions": [{**q, "rationale": "这个方向可以随便先看看再说"} for q in _VALID["questions"]]}
    llm_env("followup", "mini_followup", bad)
    with pytest.raises(ContractError) as exc:
        generate_followups(sid, store, fixture_name="mini")
    assert any("推荐依据" in r for r in exc.value.reasons)


def test_followup_flows_back_into_planner(tmp_path, llm_env):
    """TR-15.1：追问问题文本可直接驱动规划接口产出合法方案。"""
    df = pd.DataFrame({"日期": pd.to_datetime(["2024-01-01", "2024-02-01"]),
                       "区域": ["East", "West"], "品类": ["A", "B"],
                       "金额": [100.0, 200.0]})
    plan = AnalysisPlan(question="区域销售额", steps=[
        GroupByStep(step_id="g1", op="group_by",
                    params=GroupByParams(dimension="区域", metric="金额", func=AggFunc.sum))])
    store, sid, _ = _pipeline(tmp_path, df, plan, ChartType.bar)
    llm_env("followup", "mini_followup", _VALID)
    followups = generate_followups(sid, store, fixture_name="mini")
    question = followups.questions[0].text  # 「按品类下钻销售额」

    plan_fixture = {"question": question, "data_scope": "全量", "steps": [
        {"step_id": "g1", "op": "group_by",
         "params": {"dimension": "品类", "metric": "金额", "func": "sum",
                    "order": "desc", "limit": None},
         "description": "按品类汇总金额", "depends_on": []}]}
    llm_env("plan", "drill_plan", plan_fixture)
    artifact = generate_plan(sid, question, store, fixture_name="drill")
    assert artifact.plan.steps[0].params.dimension == "品类"
    assert artifact.locked is False
