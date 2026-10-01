"""T08 跨指标信号与可复核分解测试。

覆盖：
1. 变化贡献恒等式回算（sum 口径：Σδ + unexplained = ΔT，residual≈0）；
2. 率结构变化恒等式回算（within+mix+interaction = ΔR）；
3. 黄金案例②：Simpson 悖论（各组率改善、整体率下降 → Finding 引用 mix）；
4. 黄金案例①：增收不增利（规模增长 vs 盈利下降，contribution 会计证据）；
5. 启用门槛反例：不可加指标 / 无 entity_key 的 count_distinct 不产贡献，
   无分子分母双字段不产率结构分解；
6. 新视图默认 role=computation + default_hidden，不占 presentation 位。
"""
from __future__ import annotations

import io

import pandas as pd

from app.schemas.common import Confidence, SemanticType
from app.schemas.dashboard import DashboardArtifact
from app.schemas.dictionary import DataDictionary, FieldProfile, MetricSpec
from app.schemas.plan import ContributionParams, RateDecompositionParams
from app.schemas.bundle import ViewType
from app.services.bundle_executor import execute_bundle
from app.services.bundle_planner import CandidateGenerator, build_bundle
from app.services.dashboard_synthesizer import synthesize_dashboard
from app.services.engine.ops import op_contribution, op_rate_decomposition
from app.services.profiler import confirm_fields, generate_dictionary
from app.services.quality_actions import apply_decisions
from app.services.quality_checker import run_quality_checks
from app.services.storage import SessionStore


# ================================================================ 算子级恒等式


def _two_group_monthly_frame() -> pd.DataFrame:
    """2024-01/02 两个月，Region=A/B 的窗口聚合行。"""
    return pd.DataFrame([
        ("2024-01-10", "A", 100.0),
        ("2024-01-15", "B", 200.0),
        ("2024-02-10", "A", 130.0),
        ("2024-02-15", "B", 210.0),
    ], columns=["Date", "Region", "Amount"])


def test_contribution_identity_recompute():
    res = op_contribution(_two_group_monthly_frame(), ContributionParams(
        date_column="Date", metric="Amount", dimension="Region",
        func="sum", period="mom",
    ))
    out, summ = res.df, res.summary

    # 回算残差恒为 0；sum 口径下分组 δ 完全解释总量变化（unexplained≈0）
    assert abs(summ["identity_residual"]) < 1e-9
    assert abs(summ["unexplained_delta"]) < 1e-9
    assert abs(out["delta"].sum() + summ["unexplained_delta"]
               - summ["total_delta"]) < 1e-9

    assert summ["total_base_value"] == 300
    assert summ["total_current_value"] == 340
    # contribution_share 是对「变化」的贡献（合计 1），不是成员组成比
    assert abs(out["contribution_share"].sum() - 1.0) < 1e-9
    first = out.iloc[0]
    assert first["Region"] == "A" and first["delta"] == 30


def test_rate_decomposition_identity_recompute():
    # 基期：A (n=50, d=100, r=50%)，B (n=10, d=100, r=10%)；
    # 当前：A (n=55, d=100, r=55%, 权重降)，B (n=30, d=300, r=10%, 权重升)。
    df = pd.DataFrame([
        ("2024-01-10", "A", 50.0, 100.0),
        ("2024-01-15", "B", 10.0, 100.0),
        ("2024-02-10", "A", 55.0, 100.0),
        ("2024-02-15", "B", 30.0, 300.0),
    ], columns=["Date", "Region", "Conversions", "Visitors"])

    res = op_rate_decomposition(df, RateDecompositionParams(
        date_column="Date", numerator="Conversions",
        denominator="Visitors", dimension="Region", period="mom",
    ))
    summ = res.summary

    assert abs(summ["identity_residual"]) < 1e-9
    effects = (
        summ["within_effect_pp"] + summ["mix_effect_pp"]
        + summ["interaction_effect_pp"]
    ) / 100.0
    assert abs(
        effects - (summ["current_overall_rate"] - summ["base_overall_rate"])
    ) < 1e-9
    # R0=30% → R1=21.25%，即 −8.75pp；A 组率改善（within>0）但被 mix 抵消
    assert abs(summ["change_pp"] - (-8.75)) < 1e-9
    assert summ["within_effect_pp"] > 0
    assert summ["mix_effect_pp"] < 0


# ================================================================ 端到端黄金案例


def _run_pipeline(csv_bytes: bytes, tmp_path):
    """上传→语义确认→质量处理→规划→执行→合成。"""
    store = SessionStore(tmp_path / "store")
    sid = store.create_from_bytes("demo.csv", csv_bytes)["session_id"]
    dictionary = generate_dictionary(sid, store)
    pending = [
        {"name": f.name, "semantic_type": f.semantic_type.value}
        for f in dictionary.fields if not f.confirmed and not f.ignored
    ]
    if pending:
        confirm_fields(sid, pending, store)
    report = run_quality_checks(sid, store)
    decisions = {
        i.issue_id: {"action": i.suggested_action} for i in report.issues
    }
    apply_decisions(sid, decisions, store)

    snapshot = store.load_snapshot(sid)
    dictionary = DataDictionary.model_validate(
        store.read_artifact(sid, "dictionary")
    )
    bundle = build_bundle(
        dictionary, snapshot, snapshot_rows=len(snapshot), title="demo.csv"
    )
    store.write_bundle(sid, bundle.model_dump(mode="json"))
    execute_bundle(sid, store, bundle)
    artifact = synthesize_dashboard(sid, store)
    return store, sid, artifact


def _simpson_csv_bytes() -> bytes:
    """各组率改善、结构权重向低率组倾斜 → 整体率下降。

    基期：A/B 各 100 行（Sales=100），Profit/行 50/10 → R0=30%；
    当前：A 10 行 Profit 55，B 190 行 Profit 15 → R1=17%（−13pp）。
    """
    plan = {
        1: {"A": (100, 50.0), "B": (100, 10.0)},
        2: {"A": (10, 55.0), "B": (190, 15.0)},
    }
    rows = []
    for month, groups in plan.items():
        for group, (n, profit) in groups.items():
            for k in range(n):
                rows.append({
                    "Date": f"2024-{month:02d}-{1 + k % 27:02d}",
                    "Region": group,
                    "Sales": 100.0,
                    "Profit": profit,
                })
    buf = io.BytesIO()
    pd.DataFrame(rows).to_csv(buf, index=False)
    return buf.getvalue()


def test_simpson_paradox_golden_case(tmp_path):
    _, _, artifact = _run_pipeline(_simpson_csv_bytes(), tmp_path)
    assert isinstance(artifact, DashboardArtifact)

    simpson = [f for f in artifact.findings if "结构悖论" in f.title]
    assert len(simpson) == 1
    finding = simpson[0]
    assert finding.importance == "high"  # |−13pp| ≥ 5
    assert "结构权重效应" in finding.summary

    evidence_cards = [
        artifact.views[v] for v in finding.evidence_view_ids
        if v in artifact.views
    ]
    assert any(c.type is ViewType.rate_shift for c in evidence_cards)

    rate_cards = [
        v for v in artifact.views.values()
        if v.type is ViewType.rate_shift
    ]
    assert rate_cards
    for card in rate_cards:
        assert card.role == "computation"
        assert card.default_hidden is True


def _divergence_csv_bytes() -> bytes:
    """Sales +20%（10000→12000），Profit −70%（2000→600）。"""
    plan = {1: 50, 2: 60}
    rows = []
    for month, per_group in plan.items():
        profit_per_row = 20.0 if month == 1 else 5.0
        for group in ("A", "B"):
            for k in range(per_group):
                rows.append({
                    "Date": f"2024-{month:02d}-{1 + k % 27:02d}",
                    "Region": group,
                    "Sales": 100.0,
                    "Profit": profit_per_row,
                })
    buf = io.BytesIO()
    pd.DataFrame(rows).to_csv(buf, index=False)
    return buf.getvalue()


def test_scale_profit_divergence_golden_case(tmp_path):
    _, _, artifact = _run_pipeline(_divergence_csv_bytes(), tmp_path)

    divergence = [f for f in artifact.findings if "增收不增利" in f.title]
    assert len(divergence) == 1
    finding = divergence[0]
    # 贡献分解作为会计证据透出，且文案不称因果
    assert "会计拆解（非因果）" in finding.summary
    evidence_cards = [
        artifact.views[v] for v in finding.evidence_view_ids
        if v in artifact.views
    ]
    assert any(c.type is ViewType.contribution for c in evidence_cards)

    contrib_cards = [
        v for v in artifact.views.values()
        if v.type is ViewType.contribution
    ]
    assert contrib_cards
    for card in contrib_cards:
        assert card.role == "computation"
        assert card.default_hidden is True

    # 同口径下各组率同步恶化（within<0），不得误报 Simpson
    assert not [f for f in artifact.findings if "结构悖论" in f.title]


# ================================================================ 启用门槛反例


def _gate_dictionary() -> DataDictionary:
    def mkfield(name, ptype, sem, **kw) -> FieldProfile:
        return FieldProfile(
            name=name, physical_type=ptype, semantic_type=sem,
            confidence=Confidence.high, **kw
        )

    return DataDictionary(session_id="gate", complete=True, fields=[
        mkfield("Date", "datetime", SemanticType.date, is_date=True),
        mkfield("Region", "string", SemanticType.dimension, cardinality=3),
        # 不可加 mean（评分）
        mkfield("Score", "float", SemanticType.metric, is_metric=True,
                metric_spec=MetricSpec(aggregation="mean", additive=False)),
        # count_distinct 但无已确认业务键
        mkfield("OrderID", "string", SemanticType.metric, is_metric=True,
                metric_spec=MetricSpec(
                    aggregation="count_distinct", additive=False)),
        # count_distinct 且业务键已确认
        mkfield("OrderIDKey", "string", SemanticType.metric, is_metric=True,
                metric_spec=MetricSpec(
                    aggregation="count_distinct", additive=False,
                    entity_key="order_key")),
    ])


def _gate_frame() -> pd.DataFrame:
    rows = []
    for month in (1, 2):
        for i in range(40):
            rows.append({
                "Date": f"2024-{month:02d}-{1 + i % 27:02d}",
                "Region": "X" if i % 2 else "Y",
                "Score": 3.5 + (i % 5) * 0.2,
                "OrderID": f"O{month}-{i % 30}",
                "OrderIDKey": f"K{month}-{i}",
            })
    return pd.DataFrame(rows)


def test_contribution_and_rate_shift_gates():
    candidates = CandidateGenerator().generate(
        _gate_dictionary(), _gate_frame()
    )

    contrib_metrics = {
        c.metric_fields[0]
        for c in candidates if c.type is ViewType.contribution
    }
    assert "Score" not in contrib_metrics       # 率/评分不可加
    assert "OrderID" not in contrib_metrics    # count_distinct 无 entity_key
    assert "OrderIDKey" in contrib_metrics     # 业务键已确认 → 启用

    # 无分子/分母两个原始字段（仅列内值）→ 不产率结构分解
    assert not [c for c in candidates if c.type is ViewType.rate_shift]
