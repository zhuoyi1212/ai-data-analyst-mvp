"""Phase 2：DashboardArtifact 合成 + Findings 管线测试（真实上传→执行链路）。"""
from __future__ import annotations

import io

import pandas as pd

from app.schemas.bundle import ViewType
from app.schemas.dashboard import DashboardArtifact
from app.services.bundle_executor import execute_bundle
from app.services.bundle_planner import build_bundle
from app.services.dashboard_synthesizer import synthesize_dashboard
from app.services.profiler import confirm_fields, generate_dictionary
from app.services.quality_actions import apply_decisions
from app.services.quality_checker import run_quality_checks
from app.services.storage import SessionStore


def _synthetic_csv_bytes() -> bytes:
    """构造确定性数据：Furniture 整体亏损且由 Tables 拖累；Discount 与 Profit 强负相关。"""
    rows = []
    months = pd.date_range("2024-01-15", periods=6, freq="MS")
    # (子品类, 大类, 利润率, 折扣)
    subs = [
        ("Tables", "Furniture", -0.18, 0.70),
        ("Chairs", "Furniture", 0.06, 0.20),
        ("Phones", "Technology", 0.22, 0.10),
        ("Paper", "Office Supplies", 0.12, 0.05),
    ]
    for mi, month in enumerate(months):
        for sub, cat, rate, disc in subs:
            for k in range(12):
                sales = 100.0 + (mi * 37 + k * 13) % 400
                rows.append({
                    "Date": (month + pd.Timedelta(days=k)).strftime("%Y-%m-%d"),
                    "Category": cat,
                    "Sub-Category": sub,
                    "Product": f"{sub}-{'A' if k % 2 == 0 else 'B'}",
                    "Segment": "Consumer" if k % 2 else "Corporate",
                    "Sales": round(sales, 2),
                    "Profit": round(sales * rate, 2),
                    "Discount": disc,
                })
    buf = io.BytesIO()
    pd.DataFrame(rows).to_csv(buf, index=False)
    return buf.getvalue()


def _ready_dashboard(tmp_path) -> tuple[SessionStore, str]:
    store = SessionStore(tmp_path / "store")
    meta = store.create_from_bytes("pnl_demo.csv", _synthetic_csv_bytes())
    sid = meta["session_id"]
    dictionary = generate_dictionary(sid, store)
    pending = [
        {"name": f.name, "semantic_type": f.semantic_type.value}
        for f in dictionary.fields if not f.confirmed and not f.ignored
    ]
    if pending:
        confirm_fields(sid, pending, store)
    report = run_quality_checks(sid, store)
    decisions = {i.issue_id: {"action": i.suggested_action} for i in report.issues}
    apply_decisions(sid, decisions, store)

    snapshot = store.load_snapshot(sid)
    dictionary = store.read_artifact(sid, "dictionary")
    from app.schemas.dictionary import DataDictionary
    dictionary = DataDictionary.model_validate(dictionary)
    bundle = build_bundle(
        dictionary, snapshot, snapshot_rows=len(snapshot), title="pnl_demo.csv"
    )
    store.write_bundle(sid, bundle.model_dump(mode="json"))
    execute_bundle(sid, store, bundle)
    return store, sid


def test_dashboard_artifact_full_pipeline(tmp_path):
    store, sid = _ready_dashboard(tmp_path)
    artifact = synthesize_dashboard(sid, store)
    assert isinstance(artifact, DashboardArtifact)

    # 五段布局齐全；8 View 全部可追溯
    assert [s.section_id for s in artifact.sections] == [
        "overview", "trend", "structure", "diagnosis", "detail"
    ]
    section_ids = {vid for s in artifact.sections for vid in s.view_ids}
    bundle = store.read_bundle(sid)
    executed = {
        v["view_id"] for v in store.read_bundle_execution(sid)["views"]
        if v["status"] == "success"
    }
    assert section_ids == executed
    assert len(section_ids) == len(bundle["analysis_views"])

    # KPI：Sales / Profit 总览 + 利润率 KPI（=ΣProfit/ΣSales，单位 %）
    labels = [k.label for k in artifact.kpis]
    assert "Sales" in labels and "Profit" in labels and "利润率" in labels
    snap = store.load_snapshot(sid)
    expected_margin = float(snap["Profit"].sum() / snap["Sales"].sum())
    margin_kpi = next(k for k in artifact.kpis if k.label == "利润率")
    assert margin_kpi.value is not None
    assert abs(margin_kpi.value - expected_margin) < 1e-9
    assert margin_kpi.unit == "%"

    # profitability View 存在且锚定结构维度（derive_ratio 落盘 chart_spec）
    prof_ids = [
        v["view_id"] for v in bundle["analysis_views"]
        if v["type"] == ViewType.profitability.value
    ]
    assert len(prof_ids) == 1
    chart = store.view_dir(sid, prof_ids[0]) / "chart_spec.json"
    assert chart.exists()

    # 全局筛选：低中基数维度（Category 3 成员）
    filt_cols = {f.column for f in artifact.global_filters}
    assert "Category" in filt_cols
    cat_filter = next(f for f in artifact.global_filters if f.column == "Category")
    assert set(cat_filter.members) == {"Furniture", "Office Supplies", "Technology"}


def test_findings_detect_divergence_negative_member_and_correlation(tmp_path):
    store, sid = _ready_dashboard(tmp_path)
    artifact = synthesize_dashboard(sid, store)
    findings = artifact.findings

    # 1) 量利背离（对照锚点 Sub-Category：Tables 收入占比高但利润为负）
    div = [f for f in findings if f.type == "structure"]
    assert div, "应产出量利背离 finding"
    d = div[0]
    assert "Tables" in d.summary and "Sub-Category" in d.summary
    assert d.importance == "high"
    assert len(d.evidence_view_ids) == 2

    # 2) 亏损成员风险 + drilldown 探针（Tables → Product 下的负贡献产品）
    risks = [f for f in findings if f.type == "risk"]
    neg = next(f for f in risks if "Tables" in f.title)
    assert neg.importance == "high"
    assert neg.drilldown is not None
    assert neg.drilldown.child_dimension == "Product"
    kid_names = [c.name for c in neg.drilldown.top_negative_children]
    assert {"Tables-A", "Tables-B"} <= set(kid_names)
    # drilldown 筛选条件锁定到锚点成员
    assert neg.drilldown.filters[0].column == "Sub-Category"
    assert neg.drilldown.filters[0].value == "Tables"
    # 四要素：现象/位置/量化/建议
    assert all(w in neg.summary for w in ("现象", "位置", "建议"))

    # 3) Discount × Profit 强负相关 → risk 文案 + 因果免责语
    corr = [f for f in findings if f.evidence_view_ids and f.title.startswith("负相关")]
    assert corr, "应产出负相关风险 finding"
    assert "Discount" in corr[0].summary and "因果" in corr[0].summary

    # risks 是 findings 的 type==risk 子集
    assert {f.finding_id for f in artifact.risks} == {
        f.finding_id for f in findings if f.type == "risk"
    }
    # finding_id 连续编号、证据 view 真实存在
    exec_ids = {v["view_id"] for v in store.read_bundle_execution(sid)["views"]}
    for i, f in enumerate(findings, start=1):
        assert f.finding_id == f"finding_{i:02d}"
        assert set(f.evidence_view_ids) <= exec_ids


def test_probe_artifacts_are_isolated_and_budgeted(tmp_path):
    store, sid = _ready_dashboard(tmp_path)
    synthesize_dashboard(sid, store)
    probe_dir = store.bundle_dir(sid) / "probes"
    probes = [p for p in probe_dir.iterdir() if p.is_dir()]
    # KPI 利润率 1 个 + drilldown ≤1 个（预算上限 3）
    assert 1 <= len(probes) <= 3
    for p in probes:
        assert (p / "plan.json").exists()
        assert (p / "result.parquet").exists()
        assert (p / "ledger.json").exists()
