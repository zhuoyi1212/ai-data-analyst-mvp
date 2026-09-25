"""TR-13.1/13.2、TR-14.1/14.2/14.3：洞察结构、输入边界、因果护栏、接地与可信度。"""
from __future__ import annotations

import pandas as pd
import pytest

from app.schemas.common import (
    AggFunc,
    ChartType,
    Confidence,
    CorrMethod,
    InsightType,
    SemanticType,
)
from app.schemas.insight import GroundedNumber, Insight, InsightSet
from app.schemas.plan import (
    AnalysisPlan,
    CorrelationParams,
    CorrelationStep,
    GroupByParams,
    GroupByStep,
    PlanArtifact,
)
from app.services.executor import execute
from app.services.insight import generate_insights
from app.services.insight_context import build_result_digest
from app.services.insight_validator import (
    apply_confidence_policy,
    find_causal_terms,
    validate_insight_set,
)
from app.services.llm.errors import ContractError
from app.services.planner import plan_hash
from app.services.storage import SessionStore
from app.services.validator import acknowledge, run_validation

_TYPE = {"区域": SemanticType.dimension, "品类": SemanticType.dimension,
         "金额": SemanticType.metric, "数量": SemanticType.metric,
         "x": SemanticType.metric, "y": SemanticType.metric}


def _dict(columns):
    from app.schemas.dictionary import DataDictionary, FieldProfile

    return DataDictionary(session_id="x", complete=True, fields=[
        FieldProfile(name=c, physical_type="string", semantic_type=_TYPE.get(c, SemanticType.unknown),
                     confidence=Confidence.high, confirmed=True, confirmed_by_user=True)
        for c in columns])


def _pipeline(tmp_path, df, plan, chart=ChartType.bar):
    store = SessionStore(tmp_path / "storage")
    sid = store.create_from_bytes("d.csv", df.to_csv(index=False).encode())["session_id"]
    store.write_artifact(sid, "dictionary",
                         {**_dict(df.columns).model_dump(mode="json"), "session_id": sid})
    h = store.save_snapshot(sid, df)
    store.write_artifact(sid, "quality",
                         {"complete": True, "snapshot_hash": h, "snapshot_rows": len(df),
                          "issues": [], "decisions": {}})
    store.write_artifact(sid, "plan", PlanArtifact(
        question=plan.question, plan=plan, chart=chart, locked=True,
        plan_hash=plan_hash(plan)).model_dump(mode="json"))
    execute(sid, store)
    report = run_validation(sid, store)
    if report.overall == "warn":
        acknowledge(sid, store)
    return store, sid


# ------------------------------------------------------------ TR-14.1 因果词

CAUSAL_POSITIVE = [
    "广告投放导致销售额上升", "降价引起了抢购", "天气使得销量增加",
    "直播拉动了 GMV 增长", "活动提升了转化率", "优惠券驱动复购",
    "两者之间存在因果关系", "sales increased because of the ads",
    "rain caused the drop", "discounts drove revenue", "this led to growth",
    "growth due to the campaign",
]
CAUSAL_NEGATIVE = [
    "广告投放与销售额同期上升", "相关系数为 0.8（相关关系，不代表因果）",
    "高占比区域伴随更高销量", "compared with the previous period, value rose",
    "correlation does not imply causation here", "the trend line shows steady growth",
    "East 区域占比为 75%",
]


@pytest.mark.parametrize("text", CAUSAL_POSITIVE)
def test_causal_terms_flagged(text):
    assert find_causal_terms(text)


@pytest.mark.parametrize("text", CAUSAL_NEGATIVE)
def test_compliant_wording_not_flagged(text):
    assert find_causal_terms(text) == []


# ------------------------------------------------------------ 端到端接地

def _group_plan():
    return AnalysisPlan(question="各区域销售额", steps=[
        GroupByStep(step_id="g1", op="group_by",
                    params=GroupByParams(dimension="区域", metric="金额", func=AggFunc.sum))])


def _valid_group_insights():
    return {"insights": [
        {"text": "East 区域销售额最高，达到 600。", "type": "key_finding",
         "evidence_refs": ["g1"],
         "numbers": [{"value": 600, "label": "East", "ref_step": "g1", "ref_kind": "row",
                      "ref_keys": {"区域": "East"}}]},
        {"text": "West 区域销售额为 200，低于 East。", "type": "comparison",
         "evidence_refs": ["g1"],
         "numbers": [{"value": 200, "label": "West", "ref_step": "g1", "ref_kind": "row",
                      "ref_keys": {"区域": "West"}}]},
    ]}


def test_generate_grounded_insights_high_confidence(tmp_path, llm_env):
    df = pd.DataFrame({"区域": ["East"] * 3 + ["West"],
                       "金额": [100.0, 200.0, 300.0, 200.0]})
    store, sid = _pipeline(tmp_path, df, _group_plan())
    llm_env("insights", "mini_insights", _valid_group_insights())
    result = generate_insights(sid, store, fixture_name="mini")
    assert len(result.insights) == 2
    assert all(i.confidence == Confidence.high for i in result.insights)
    assert all(not i.needs_further_validation for i in result.insights)
    artifact = store.read_artifact(sid, "insights")
    assert artifact["digest_row_cap"] == 50  # TR-13.2


def test_tampered_number_blocked(tmp_path, llm_env):
    df = pd.DataFrame({"区域": ["East", "West"], "金额": [600.0, 200.0]})
    store, sid = _pipeline(tmp_path, df, _group_plan())
    bad = _valid_group_insights()
    bad["insights"][0]["text"] = "East 区域销售额达到 999。"  # 999 在引擎结果中不存在
    bad["insights"][0]["numbers"] = [{
        "value": 999, "label": "East", "ref_step": "g1", "ref_kind": "row",
        "ref_keys": {"区域": "East"}}]
    llm_env("insights", "mini_insights", {"responses": [bad, bad, bad]})
    with pytest.raises(ContractError) as exc:
        generate_insights(sid, store, fixture_name="mini")
    assert any("接地" in r for r in exc.value.reasons)


def test_business_repair_within_two_attempts(tmp_path, llm_env):
    df = pd.DataFrame({"区域": ["East", "West"], "金额": [600.0, 200.0]})
    store, sid = _pipeline(tmp_path, df, _group_plan())
    bad = _valid_group_insights()
    bad["insights"][0]["numbers"][0]["value"] = 999  # 与引擎值不一致
    llm_env("insights", "mini_insights", {"responses": [bad, _valid_group_insights()]})
    result = generate_insights(sid, store, fixture_name="mini")
    assert result.insights[0].numbers[0].value == 600


def test_correlation_requires_disclaimer_and_caps_confidence(tmp_path, llm_env):
    n = 100
    df = pd.DataFrame({"x": [float(i) for i in range(n)],
                       "y": [float(i) * 2 for i in range(n)]})
    plan = AnalysisPlan(question="x 与 y 的关系", steps=[
        CorrelationStep(step_id="c1", op="correlation",
                        params=CorrelationParams(column_x="x", column_y="y",
                                                 method=CorrMethod.pearson))])
    store, sid = _pipeline(tmp_path, df, plan, ChartType.scatter)
    # 缺免责语 → 两次修复后给出合规版本
    no_disc = {"insights": [
        {"text": "x 与 y 的相关系数为 1，呈完全正相关。", "type": "key_finding",
         "evidence_refs": ["c1"], "numbers": [
             {"value": 1, "label": "相关系数", "ref_step": "c1", "ref_kind": "summary",
              "ref_keys": {"field": "coefficient"}}]},
        {"text": "样本对数量为 100。", "type": "key_finding", "evidence_refs": ["c1", ] ,
         "numbers": [{"value": 100, "label": "n", "ref_step": "c1", "ref_kind": "summary",
                      "ref_keys": {"field": "n"}}]},
    ]}
    good = {"insights": [
        {"text": "x 与 y 的相关系数为 1，呈完全正相关（相关关系，不代表因果）。",
         "type": "key_finding", "evidence_refs": ["c1"], "numbers": [
             {"value": 1, "label": "相关系数", "ref_step": "c1", "ref_kind": "summary",
              "ref_keys": {"field": "coefficient"}}]},
        {"text": "样本对数量为 100（相关关系，不代表因果）。", "type": "key_finding",
         "evidence_refs": ["c1"], "numbers": [
             {"value": 100, "label": "n", "ref_step": "c1", "ref_kind": "summary",
              "ref_keys": {"field": "n"}}]},
    ]}
    llm_env("insights", "mini_insights", {"responses": [no_disc, good]})
    result = generate_insights(sid, store, fixture_name="mini")
    assert all(i.disclaimer == "相关关系，不代表因果" for i in result.insights)
    # 相关分析风险 → 置信度封顶 medium 且标注需进一步验证
    assert all(i.confidence == Confidence.medium for i in result.insights)
    assert all(i.needs_further_validation for i in result.insights)


def test_digest_payload_has_no_raw_rows(tmp_path, llm_env):
    """TR-13.2：60 个分组时输入载荷截断到 50 行，且不含全量明细。"""
    df = pd.DataFrame({"区域": [f"R{i % 60}" for i in range(600)],
                       "金额": [float(i) for i in range(600)]})
    cap_fixture = {"insights": [
        {"text": "R59 区域销售额最高，为 3290。", "type": "key_finding",
         "evidence_refs": ["g1"], "numbers": [
             {"value": 3290, "label": "R59", "ref_step": "g1", "ref_kind": "row",
              "ref_keys": {"区域": "R59"}}]},
        {"text": "R58 区域销售额为 3280。", "type": "comparison",
         "evidence_refs": ["g1"], "numbers": [
             {"value": 3280, "label": "R58", "ref_step": "g1", "ref_kind": "row",
              "ref_keys": {"区域": "R58"}}]},
    ]}
    store, sid = _pipeline(tmp_path, df, _group_plan())
    llm_env("insights", "cap_insights", cap_fixture)
    generate_insights(sid, store, fixture_name="cap")
    payload = store.read_artifact(sid, "insights")["result_digest"]
    assert len(payload["result_rows"]) == 50
    assert payload["participating_rows"] == 600


# ------------------------------------------------------------ TR-14.3 可信度策略

def _bare_digest(risks):
    from app.services.insight_context import ResultDigest

    return ResultDigest(
        question="q", terminal_op="aggregate", chart="metric",
        steps=[{"step_id": "s1"}], columns=["value"], rows=[], result_summary={},
        risks=risks)


def test_confidence_policy_levels():
    ins = Insight(text="占位洞察文本，数值为 1", type=InsightType.key_finding,
                  evidence_refs=["s1"], numbers=[
                      GroundedNumber(value=1, ref_step="s1", ref_kind="metric")])
    high = apply_confidence_policy(ins.model_copy(deep=True), _bare_digest([]))
    assert high.confidence == Confidence.high and not high.needs_further_validation

    mid = apply_confidence_policy(ins.model_copy(deep=True),
                                  _bare_digest(["涉及相关分析，仅反映相关关系"]))
    assert mid.confidence == Confidence.medium and mid.needs_further_validation

    low = apply_confidence_policy(
        ins.model_copy(deep=True),
        _bare_digest(["涉及相关分析，仅反映相关关系", "样本量较小（n=8，少于 30）"]))
    assert low.confidence == Confidence.low and low.needs_further_validation


def test_growth_direction_mismatch_detected(tmp_path):
    """增长率为负但文本说上升 → 拦截（TR-14 统计方向一致性）。"""
    from app.schemas.common import ComparePeriod
    from app.schemas.plan import PeriodCompareParams, PeriodCompareStep
    from app.services.ledger_loader import load_execution_context

    df = pd.DataFrame({
        "日期": pd.to_datetime(["2024-01-10", "2024-02-10"]),
        "金额": [200.0, 100.0],
    })
    plan = AnalysisPlan(question="环比变化", steps=[
        PeriodCompareStep(step_id="p1", op="period_compare",
                          params=PeriodCompareParams(date_column="日期",
                                                     period=ComparePeriod.mom,
                                                     metric="金额", func=AggFunc.sum))])
    store, sid = _pipeline(tmp_path, df, plan, ChartType.metric_compare)
    ledger, pa, validation, result = load_execution_context(sid, store)
    digest = build_result_digest(ledger, pa, result, validation)

    def _insight(text):
        return Insight(text=text, type=InsightType.trend, evidence_refs=["p1"],
                       numbers=[GroundedNumber(value=-50, ref_step="p1", ref_kind="summary",
                                                ref_keys={"field": "growth_pct"})])

    bad = InsightSet(insights=[_insight("销售额环比变化 -50%，整体呈上升趋势。"),
                               _insight("本期相对上期变化 -50%，走势继续向上。")])
    errors = validate_insight_set(bad, digest)
    assert any("方向" in e for e in errors)

    good = InsightSet(insights=[_insight("销售额环比变化 -50%，较上期明显下降。"),
                                _insight("本期相对上期变化 -50%，规模回落。")])
    assert validate_insight_set(good, digest) == []
