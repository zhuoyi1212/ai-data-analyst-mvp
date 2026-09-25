"""TR-17：5 份示例数据的离线全闭环 E2E（replay 固件，无 key 无网络）。

Upload → Semantic Profile（含用户确认）→ Quality → Questions → Plan →
Execute → Validate → Insight → Follow-up，并断言关键聚合与黄金值一致、
洞察数字全部接地、无因果性表述。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app.schemas.dictionary import DataDictionary, FieldProfile
from app.schemas.common import Confidence, SemanticType
from app.services.executor import execute
from app.services.followup import generate_followups
from app.services.insight import generate_insights
from app.services.insight_validator import find_causal_terms
from app.services.llm.errors import ContractError
from app.services.parser import friendly_dtype
from app.services.planner import confirm_plan, generate_plan
from app.services.profiler import confirm_fields, generate_dictionary
from app.services.quality_actions import apply_decisions
from app.services.quality_checker import run_quality_checks
from app.services.recommender import recommend
from app.services.storage import SessionStore
from app.services.validator import acknowledge, run_validation
from scripts.build_e2e_fixtures import DATASET_META
from scripts.generate_sample_data import DATASETS_CONFIG, GOLDEN_PATH

GOLDEN = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["datasets"]

# 每数据集规范方案对应的黄金值键与容差
CANONICAL_GOLDEN_KEY = {
    "sales_orders": "amount_sum",
    "operations_daily": "dau_mean",
    "marketing_campaigns": "spend_sum",
    "inventory_movements": "ending_stock_mean",
    "customer_tickets": "handle_hours_mean",
}


def test_committed_fixtures_present():
    """前端/评审在 replay 模式下即开即用所需的全部固件。"""
    for name in DATASETS_CONFIG:
        assert (settings.fixture_dir / "profile" / f"{name}.json").exists()
        for stage, suffix in [("questions", "_questions"), ("plan", "_plan"),
                             ("insights", "_insights"), ("followup", "_followup")]:
            assert (settings.fixture_dir / stage / f"{name}{suffix}.json").exists()


@pytest.mark.parametrize("name", list(DATASETS_CONFIG))
def test_full_pipeline_offline(name: str, tmp_path: Path):
    golden = GOLDEN[name]
    canon = DATASET_META[name]["canonical"]
    medium_col = next(iter(DATASET_META[name]["medium"]))
    store = SessionStore(tmp_path / "store")

    # 1. Upload（使用内置示例）
    meta = store.create_from_sample(golden["filename"])
    sid = meta["session_id"]

    # 2. Semantic Profile：回放固件含 1 个 medium 字段 → 必须用户确认
    dictionary = generate_dictionary(sid, store)
    medium_fields = [f for f in dictionary.fields if f.confidence != Confidence.high]
    assert any(f.name == medium_col for f in medium_fields)
    assert dictionary.complete is False
    dictionary = confirm_fields(sid, [{
        "name": medium_col,
        "semantic_type": DATASETS_CONFIG[name]["semantics"][medium_col].value,
        "meaning": "用户确认的字段含义",
    }], store)
    assert dictionary.complete is True

    # 3. Data Quality：检测 + 按黄金决策处理
    report = run_quality_checks(sid, store)
    counts: dict[str, int] = {}
    for issue in report.issues:
        counts[issue.type.value] = counts.get(issue.type.value, 0) + 1
    assert counts == golden["issue_counts"]
    report = apply_decisions(sid, golden["decisions"], store)
    assert report.snapshot_rows == golden["snapshot_rows"]
    assert report.snapshot_hash == golden["snapshot_hash"]

    # 4. Questions：7 条、≥4 分类、首条即规范问题
    qs = recommend(sid, store)
    assert len(qs.questions) == 7
    assert len({q.category for q in qs.questions}) >= 4
    assert all(q.rationale for q in qs.questions)
    assert qs.questions[0].text == canon["question"]

    # 5. Plan → Confirm
    artifact = generate_plan(sid, qs.questions[0].text, store)
    assert artifact.plan.steps[-1].op == "aggregate"
    assert artifact.locked is False
    artifact = confirm_plan(sid, store)
    assert artifact.locked is True and artifact.plan_hash

    # 6. Execute：数值唯一来源是引擎
    ledger = execute(sid, store)
    assert ledger.snapshot_hash == golden["snapshot_hash"]
    value = ledger.steps[-1].summary["value"]
    expected = golden["golden_values"][CANONICAL_GOLDEN_KEY[name]]
    assert abs(value - expected) < 1e-4

    # 7. Validate：五项检查整体 pass
    validation = run_validation(sid, store)
    assert validation.overall == "pass"
    assert len(validation.items) == 5
    assert all(i.level.value == "pass" for i in validation.items)

    # 8. Insight：2 条、数字接地、高置信、无因果词
    insights = generate_insights(sid, store)
    assert len(insights.insights) == 2
    for ins in insights.insights:
        assert find_causal_terms(ins.text) == []
        assert ins.confidence == Confidence.high
        assert ins.needs_further_validation is False
        assert ins.evidence_refs == ["agg"]
        assert len(ins.numbers) >= 1
        for num in ins.numbers:
            point = num  # 接地引用必须指向真实步骤与指标
            assert point.ref_step == "agg"
            assert point.ref_kind == "metric"
            assert abs(point.value - value) < 0.02

    # 9. Follow-up：3 条接地追问
    followups = generate_followups(sid, store)
    assert len(followups.questions) == 3
    assert all(len(q.rationale) >= 10 and q.fields for q in followups.questions)

    # 10. 关键产物全部落盘可追溯
    for artifact_name in ("dictionary", "quality", "questions", "plan",
                          "ledger", "validation", "insights", "followups"):
        assert store.has_artifact(sid, artifact_name)


# ---------------------------------------------------------------- 畸形固件负例

def test_malformed_fixtures_raise_contract_error(tmp_path: Path, llm_env):
    """5 个 LLM 阶段的畸形输出必须显式失败（ContractError），绝不静默编造。"""
    store = SessionStore(tmp_path / "store")
    golden = GOLDEN["sales_orders"]
    meta = store.create_from_sample(golden["filename"])
    sid = meta["session_id"]

    # --- profile：非法枚举，schema 校验失败 ---
    llm_env("profile", "bad_profile", {"fields": [{
        "name": "金额", "semantic_type": "nonsense",
        "meaning": "x", "candidates": [], "confidence": "high"}]})
    with pytest.raises(ContractError):
        generate_dictionary(sid, store, fixture_name="bad_profile")

    # 直接写入完整字典以推进后续阶段
    import pandas as pd
    df = store.load_original(sid)
    fields = [
        FieldProfile(name=col, physical_type=friendly_dtype(df[col]),
                     semantic_type=sem, meaning="x",
                     confidence=Confidence.high, confirmed=True, confirmed_by_user=True)
        for col, sem in DATASETS_CONFIG["sales_orders"]["semantics"].items()
    ]
    dictionary = DataDictionary(session_id=sid, fields=fields, complete=True)
    store.write_artifact(sid, "dictionary", dictionary.model_dump(mode="json"))

    # --- questions：数量不足（2 < 6），业务校验失败 ---
    q = {"text": "总额是多少", "category": "overview",
         "rationale": "「金额」是核心指标", "fields": ["金额"],
         "target_op": "aggregate", "confidence": "high"}
    llm_env("questions", "badq_questions", {"questions": [q, q]})
    with pytest.raises(ContractError):
        recommend(sid, store, fixture_name="badq")

    # --- plan：白名单外算子，schema 校验失败 ---
    llm_env("plan", "badp_plan", {
        "question": "任意问题", "data_scope": "",
        "steps": [{"step_id": "s1", "op": "run_sql", "params": {"sql": "SELECT 1"},
                   "description": "违规", "depends_on": []}],
        "expected_shape": "", "chart_hint": "table"})
    with pytest.raises(ContractError):
        generate_plan(sid, "任意问题", store, fixture_name="badp")

    # 推进到执行后：质量处理 + 直接锁定规范方案（不经过 LLM）
    run_quality_checks(sid, store)
    apply_decisions(sid, golden["decisions"], store)
    raw_plan = {
        "question": "全部有效订单的销售总额是多少？",
        "data_scope": "质量处理后的全部数据",
        "steps": [{"step_id": "agg", "op": "aggregate",
                   "params": {"column": "金额", "func": "sum"},
                   "description": "金额求和", "depends_on": []}],
        "expected_shape": "指标卡", "chart_hint": "metric"}
    confirm_plan(sid, store, raw_plan)
    execute(sid, store)
    run_validation(sid, store)

    # --- insights：数字 999999 未接地，业务校验失败 ---
    def _ins(text: str) -> dict:
        return {
            "text": text, "type": "key_finding", "evidence_refs": ["agg"],
            "numbers": [{"value": 999999, "label": "虚构数字",
                         "ref_step": "agg", "ref_kind": "metric", "ref_keys": {}}],
            "confidence": "high", "confidence_reason": "",
            "needs_further_validation": False, "disclaimer": None,
        }
    llm_env("insights", "badi_insights", {"insights": [
        _ins("销售总额高达 999999，远超实际。"),
        _ins("另一个虚构结论数字 999999 同样不存在。")]})
    with pytest.raises(ContractError):
        generate_insights(sid, store, fixture_name="badi")

    # --- followups：推荐依据未接地（无字段名/特征词），业务校验失败 ---
    fq = {"text": "随便看看", "rationale": "没有任何字段依据可言",
          "fields": ["金额"], "target_op": "aggregate", "plan_hint": {}}
    llm_env("followup", "badf_followup", {"questions": [fq, fq, fq]})
    with pytest.raises(ContractError):
        generate_followups(sid, store, fixture_name="badf")


# ---------------------------------------------------------------- 追问多轮闭环

@pytest.mark.parametrize("name", list(DATASETS_CONFIG))
def test_followup_rounds_offline(name: str, tmp_path: Path):
    """每条追问都能生成与问题匹配的方案、执行并产出接地洞察（离线回放变体固件）。"""
    golden = GOLDEN[name]
    canon = DATASET_META[name]["canonical"]
    medium_col = next(iter(DATASET_META[name]["medium"]))
    store = SessionStore(tmp_path / "store")
    meta = store.create_from_sample(golden["filename"])
    sid = meta["session_id"]

    # 完成 canonical 全流程
    dictionary = generate_dictionary(sid, store)
    confirm_fields(sid, [{
        "name": medium_col,
        "semantic_type": DATASETS_CONFIG[name]["semantics"][medium_col].value,
        "meaning": "用户确认的字段含义",
    }], store)
    run_quality_checks(sid, store)
    apply_decisions(sid, golden["decisions"], store)
    qs = recommend(sid, store)
    generate_plan(sid, qs.questions[0].text, store)
    confirm_plan(sid, store)
    execute(sid, store)
    run_validation(sid, store)
    generate_insights(sid, store)
    followups = generate_followups(sid, store)

    # 对每条追问：生成方案必须与 canonical aggregate 不同（变体固件命中）
    canonical_op = "aggregate"
    for q in followups.questions:
        artifact = generate_plan(sid, q.text, store)
        # 追问方案不应回退为 canonical 的 aggregate（除非该追问本身就是 aggregate）
        if q.target_op != "aggregate":
            assert artifact.plan.steps[-1].op == q.target_op, (
                f"{name} 追问「{q.text}」应得到 {q.target_op} 方案"
            )
        assert artifact.plan.steps[-1].op != canonical_op or q.target_op == "aggregate"
        assert artifact.plan.question == q.text

        confirm_plan(sid, store)
        execute(sid, store)
        validation = run_validation(sid, store)
        assert validation.overall != "fail"
        if validation.overall == "warn":
            acknowledge(sid, store)

        insights = generate_insights(sid, store)
        assert len(insights.insights) >= 2
        for ins in insights.insights:
            assert find_causal_terms(ins.text) == []
            assert ins.evidence_refs  # 必须引用真实步骤
            for num in ins.numbers:
                assert num.ref_step == artifact.plan.steps[-1].step_id
            # 相关分析必须带因果免责
            if q.target_op == "correlation":
                assert "相关关系，不代表因果" in ins.text
