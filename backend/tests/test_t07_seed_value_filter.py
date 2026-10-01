"""T07：Seed 与结果价值筛选。

验收：
- r=0 / 弱 r 不占默认视图（section），只作 computation 证据任务默认隐藏，
  阴性结果仍可从视图字典返回；
- 无信号数据（无异常、无明显变化）不凑 Findings；
- 双指标对齐趋势恒定在 Seed，不被槽位挤掉；
- 去重键 (metric/scope/period/question) 合并指向同一问题的 Findings。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import pytest

from app.schemas.bundle import ViewType
from app.schemas.dashboard import Finding
from app.services.bundle_executor import execute_bundle
from app.services.bundle_planner import build_bundle
from app.services.dashboard_insight import merge_same_question
from app.services.dashboard_synthesizer import synthesize_dashboard
from app.services.profiler import confirm_fields, generate_dictionary
from app.services.quality_actions import apply_decisions
from app.services.quality_checker import run_quality_checks
from app.services.storage import SessionStore


# ----------------------------------------------------------------- helpers


def _build_artifact(df: pd.DataFrame):
    """上传→字典→确认→质量→Bundle→执行→合成，返回 (store, sid, artifact)。"""
    store = SessionStore(Path(tempfile.mkdtemp()) / "store")
    sid = store.create_from_bytes(
        "data.csv", df.to_csv(index=False).encode()
    )["session_id"]
    dictionary = generate_dictionary(sid, store)
    pending = [
        {"name": f.name, "semantic_type": f.semantic_type.value}
        for f in dictionary.fields if not f.confirmed and not f.ignored
    ]
    if pending:
        confirm_fields(sid, pending, store)
    report = run_quality_checks(sid, store)
    apply_decisions(
        sid,
        {i.issue_id: {"action": i.suggested_action} for i in report.issues},
        store,
    )
    snapshot = store.load_snapshot(sid)
    from app.schemas.dictionary import DataDictionary
    dictionary = DataDictionary.model_validate(
        store.read_artifact(sid, "dictionary")
    )
    bundle = build_bundle(
        dictionary, snapshot, snapshot_rows=len(snapshot), title="data.csv"
    )
    store.write_bundle(sid, bundle.model_dump(mode="json"))
    execute_bundle(sid, store, bundle)
    return store, sid, synthesize_dashboard(sid, store)


def _zero_r_df() -> pd.DataFrame:
    # [0,1,1,0] 在每个长度 4 的对称块内与线性 X 严格正交 → Pearson r=0
    y_pattern = [0, 1, 1, 0]
    return pd.DataFrame({
        "日期": pd.date_range("2024-01-01", periods=100, freq="D"),
        "区域": (["华东", "华南", "华北", "华北"] * 25),
        "X": list(range(100)),
        "Y": [y_pattern[i % 4] for i in range(100)],
    })


# ----------------------------------------------------------------- 1. r=0


def test_r_zero_relationship_not_in_default_sections():
    _, _, artifact = _build_artifact(_zero_r_df())
    rel_cards = [c for c in artifact.views.values() if c.type == "relationship"]
    assert rel_cards, "r=0 时阴性证据任务仍应执行并保留"
    rel = rel_cards[0]

    # 不占任何默认 section
    slotted = {vid for s in artifact.sections for vid in s.view_ids}
    assert rel.view_id not in slotted
    assert rel.role == "computation"
    assert rel.default_hidden is True
    assert rel.hide_reasons, "应给出具体隐藏理由（弱相关）"

    # presentation 卡片恒可见
    presentation = [c for c in artifact.views.values() if c.role == "presentation"]
    assert presentation
    assert all(c.default_hidden is False and c.hide_reasons == []
               for c in presentation)

    # r=0 不产相关性 Finding
    assert not [
        f for f in artifact.findings
        if any(vid == rel.view_id for vid in f.evidence_view_ids)
    ]


def test_negative_relationship_result_still_available_in_view_dictionary():
    """用户明确问到时仍可返回阴性结果：视图字典里卡片成功且携带数据。"""
    _, _, artifact = _build_artifact(_zero_r_df())
    rel = next(c for c in artifact.views.values() if c.type == "relationship")
    assert rel.status == "success"
    assert rel.data is not None
    assert rel.chart_spec is not None


# ----------------------------------------------------------------- 2. 无信号


def test_no_signal_data_produces_no_findings():
    # 单一可加指标，四个区域、各月完全相同：无离群、无环比变化、无指标对
    months = pd.date_range("2024-01-01", periods=4, freq="MS")
    rows = []
    for m in months:
        for region in ["华东", "华南", "华北", "西南"]:
            rows.append({"日期": m, "区域": region, "销售额": 100.0})
    _, _, artifact = _build_artifact(pd.DataFrame(rows))

    assert artifact.findings == []
    assert artifact.risks == []
    # 核心视图仍然存在（阴性不等于空态）
    assert artifact.state == "ready"


# ----------------------------------------------------------------- 3. 双趋势


def test_dual_metric_trends_always_in_seed():
    dates = pd.date_range("2024-01-01", periods=90, freq="D")
    df = pd.DataFrame({
        "日期": dates.repeat(2)[:180],
        "区域": (["华东", "华南", "华北"] * 60)[:180],
        "Sales": [100.0 + i % 50 for i in range(180)],
        "Profit": [20.0 - (i % 30) for i in range(180)],
    })
    _, _, artifact = _build_artifact(df)

    presentation = [c for c in artifact.views.values()
                    if c.role == "presentation"]
    trends = [c for c in presentation if c.type == "trend"]
    trend_metrics = {c.metric_label for c in trends}
    assert {"Sales", "Profit"} <= trend_metrics, "双指标对齐趋势必须都在 Seed"
    overviews = [c for c in presentation if c.type == "overview"]
    assert {c.metric_label for c in overviews} == {"Sales", "Profit"}

    # 两个趋势都在默认 trend section
    trend_slot = next(s for s in artifact.sections if s.section_id == "trend")
    assert {c.view_id for c in trends} == set(trend_slot.view_ids)


# ----------------------------------------------------------------- 4. 合并


def _quick_finding(
    title: str, ftype: str, importance: str,
    evidence: list[str], drilldown=None,
) -> Finding:
    return Finding(
        finding_id="", title=title, summary=f"现象：{title}。建议：关注。",
        type=ftype, evidence_view_ids=evidence, importance=importance,
        drilldown=drilldown,
    )


def test_merge_same_question_unifies_by_dedup_key():
    key = ("Profit", "scope1", "", "profit_problem:Sub-Category:Tables")
    f1 = _quick_finding("量利背离", "structure", "high", ["view_05"])
    f2 = _quick_finding("亏损风险", "risk", "high", ["view_08"])
    other = _quick_finding("另一个问题", "risk", "medium", ["view_09"])

    merged = merge_same_question([(f1, key), (f2, key),
                                  (other, ("X", "scope1", "", "other"))])
    assert len(merged) == 2
    primary = merged[0]
    # 证据并集、最高重要性、结构类型保留
    assert primary.evidence_view_ids == ["view_05", "view_08"]
    assert primary.importance == "high"
    assert primary.type == "structure"
    assert "亏损风险" in primary.summary  # 另一条以「另见」补充，信息不丢


def test_divergence_and_risk_merge_end_to_end(tmp_path):
    """同一成员的量利背离与亏损风险在结果层合并为一个 Finding。"""
    from test_dashboard_pipeline import _ready_dashboard
    store, sid = _ready_dashboard(tmp_path)
    artifact = synthesize_dashboard(sid, store)

    structure = [f for f in artifact.findings if f.type == "structure"]
    assert len(structure) == 1
    d = structure[0]
    # 不再有独立的 Tables 风险 Finding（已合并）
    tables_risks = [
        f for f in artifact.findings
        if f.type == "risk" and "Tables" in f.title
    ]
    assert tables_risks == []
    assert "亏损风险" in d.summary
    assert len(d.evidence_view_ids) == 2


# ----------------------------------------------------- 5. 真实示例集回归


_ALL_SAMPLE_DATASETS = [
    "sales_orders", "operations_daily", "marketing_campaigns",
    "inventory_movements", "customer_tickets",
]


@pytest.mark.parametrize("dataset", _ALL_SAMPLE_DATASETS)
def test_seed_includes_aligned_trends_for_two_primary_metrics(
    tmp_path, dataset
):
    """有两个主指标就必须有两个 Seed 趋势（T07 第三条验收，真实数据回归）。"""
    from test_bundle_planner import _ready_session
    store, sid, dictionary, snapshot = _ready_session(dataset, tmp_path)
    bundle = build_bundle(
        dictionary, snapshot, snapshot_rows=len(snapshot), title=dataset
    )
    presentation = [
        v for v in bundle.analysis_views if v.role == "presentation"
    ]
    trends = {
        v.metric_fields[0] for v in presentation
        if v.type is ViewType.trend and v.metric_fields
    }
    assert set(bundle.primary_metrics) <= trends, (
        f"{dataset} 两个主指标 {bundle.primary_metrics} 的对齐趋势缺失，"
        f"实际 Seed 趋势：{sorted(trends)}"
    )


def test_m1_trend_finding_detected_when_secondary_metric_declines():
    """主指标 Sales 平稳、第二指标 Profit 逐月下滑时必须产出 Profit 下滑 Finding。"""
    months = pd.date_range("2024-01-01", periods=5, freq="MS")
    profit_levels = [1000, 700, 400, 100, -200]
    rows = []
    for mi, m in enumerate(months):
        for region in ["华东", "华南", "华北", "西南"]:
            for d in range(5):
                rows.append({
                    "日期": m + pd.Timedelta(days=d), "区域": region,
                    "Sales": 500.0, "Profit": float(profit_levels[mi]),
                })
    _, _, artifact = _build_artifact(pd.DataFrame(rows))

    declines = [f for f in artifact.findings if f.type == "decline"]
    assert any("Profit" in f.title for f in declines), (
        f"第二核心指标 Profit 明显下滑却零信号，实际："
        f"{[f.title for f in artifact.findings]}"
    )
    # Sales 平稳，不凑增长 Finding
    assert not any("Sales" in f.title for f in declines)
