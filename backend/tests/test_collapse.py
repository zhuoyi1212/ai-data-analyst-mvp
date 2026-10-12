"""维度折叠：同指标 × 同类型 × 不同维度 → 一张图 + 维度选择器。"""
from __future__ import annotations

from app.schemas.bundle import ViewStatus, ViewType
from app.schemas.common import ChartType
from app.schemas.dashboard import ChartSpec, ViewCard
from app.services.dashboard_synthesizer import _build_collapse_groups


def _card(view_id: str, metric: str, dim: str, vtype: ViewType) -> ViewCard:
    return ViewCard(
        view_id=view_id, title=f"t{view_id}", question="q", type=vtype,
        status=ViewStatus.success, consumable=True, role="presentation",
        default_hidden=False, metric_label=metric,
        chart_spec=ChartSpec(type=ChartType.bar, title="t", dimension=dim),
    )


def test_collapse_groups_fold_same_metric_dimensions():
    cards = {
        "v1": _card("v1", "Profit", "Segment", ViewType.comparison),
        "v2": _card("v2", "Profit", "Category", ViewType.comparison),
        "v3": _card("v3", "Profit", "Region", ViewType.comparison),
        "v4": _card("v4", "Sales", "Segment", ViewType.comparison),   # 单维度，不折叠
        "v5": _card("v5", "Profit", "Segment", ViewType.breakdown),   # 不同类型，单独组
    }
    groups = _build_collapse_groups(cards)

    assert len(groups) == 1
    g = groups[0]
    assert g.label == "利润对比"
    assert [m.view_id for m in g.members] == ["v1", "v2", "v3"]
    assert [m.dimension_label for m in g.members] == ["客户细分", "类别", "区域"]


def test_profitability_groups_keep_ratio_definitions_separate():
    cards = {}
    for vid, dimension, metric in [
        ("v1", "Segment", "Profit/Sales"),
        ("v2", "Category", "Profit/Sales"),
        ("v3", "Region", "Profit/Cost"),
    ]:
        card = _card(vid, "Profit", dimension, ViewType.profitability)
        card.chart_spec.metric = metric
        cards[vid] = card
    groups = _build_collapse_groups(cards)
    assert len(groups) == 1
    assert [member.view_id for member in groups[0].members] == ["v1", "v2"]


def test_rankings_fold_metric_and_direction_variants():
    cards = {}
    for vid, metric, direction in [
        ("v1", "Profit", "Top"), ("v2", "Profit", "Bottom"),
        ("v3", "Sales", "Top"), ("v4", "Sales", "Bottom"),
    ]:
        card = _card(vid, metric, "Customer Name", ViewType.ranking)
        card.title = f"Customer Name {metric} {direction} 5"
        cards[vid] = card
    groups = _build_collapse_groups(cards)
    assert len(groups) == 1
    assert groups[0].label == "客户名称排名"
    assert [f"{member.metric_label} · {member.variant_label}" for member in groups[0].members] == [
        "利润 · 前 5", "利润 · 后 5", "销售额 · 前 5", "销售额 · 后 5",
    ]


def test_groups_require_known_probe_scope_and_include_hidden_variants():
    cards = {vid: _card(vid, "Profit", dim, ViewType.comparison)
             for vid, dim in [("v1", "Segment"), ("v2", "Category"), ("v3", "Region")]}
    cards["v2"].ref_type = "probe"
    cards["v3"].consumable = False
    assert _build_collapse_groups(cards) == []
    cards["v2"].ref_type = "view"
    cards["v2"].default_hidden = True
    assert len(_build_collapse_groups(cards)) == 1


def test_cached_presentation_refresh_preserves_evidence_and_numbers():
    from app.schemas.dashboard import (
        ColumnSchema, DashboardArtifact, DashboardLayout, DashboardLayoutItem,
        KPI, ViewDataEnvelope,
    )
    from app.services.dashboard_synthesizer import refresh_dashboard_presentation

    cards = {vid: _card(vid, "Profit", dim, ViewType.comparison)
             for vid, dim in [("v1", "Segment"), ("v2", "Category")]}
    overview = _card("v3", "Quantity", "", ViewType.overview)
    overview.chart_spec = None
    overview.data = ViewDataEnvelope(
        kind="aggregate", columns=[ColumnSchema(name="value", dtype="number", label="销售数量")],
        rows=[{"value": 123}],
    )
    cards["v3"] = overview
    artifact = DashboardArtifact(
        bundle_id="bundle", title="demo", state="ready", views=cards,
        kpis=[KPI(label="利润", value=42)],
        layout=DashboardLayout(generated_at="2026-10-12", items=[
            DashboardLayoutItem(item_id="v1", role="supporting", col_span=4, row_span=1,
                                order=0, rationale="证据", default_hidden=True),
        ]),
    )
    original = artifact.model_dump()
    refreshed = refresh_dashboard_presentation(artifact)
    assert len(refreshed.collapse_groups) == 1
    assert [kpi.label for kpi in refreshed.kpis] == ["利润", "销售数量"]
    assert [kpi.value for kpi in refreshed.kpis] == [42, 123]
    assert refreshed.views == artifact.views
    assert refreshed.layout == artifact.layout
    assert artifact.model_dump() == original
    assert refresh_dashboard_presentation(refreshed) == refreshed


def test_probe_scopes_are_explicit_and_duplicate_results_keep_aliases():
    from app.schemas.dashboard import ColumnSchema, ViewDataEnvelope
    cards = {}
    for vid, dimension, scope in [
        ("v1", "Category", "[]"), ("p1", "Category", "[]"),
        ("p2", "Region", "Furniture"), ("p3", "Category", "Furniture"),
    ]:
        card = _card(vid, "Profit", dimension, ViewType.comparison)
        card.scope_key = scope
        card.scope_label = "全部数据" if scope == "[]" else "类别=Furniture"
        card.ref_type = "view" if vid == "v1" else "probe"
        card.role = "presentation" if vid == "v1" else "computation"
        card.default_hidden = vid != "v1"
        card.chart_spec.x_field = dimension
        card.chart_spec.metric = "Profit"
        card.data = ViewDataEnvelope(kind="aggregate", columns=[ColumnSchema(name="value", dtype="number")],
                                     rows=[{dimension: "A", "value": 10}, {dimension: "B", "value": 20}])
        cards[vid] = card
    groups = _build_collapse_groups(cards)
    assert len(groups) == 1
    assert len(groups[0].members) == 3
    assert groups[0].members[0].view_ids == ["v1", "p1"]
    assert {m.scope_label for m in groups[0].members} == {"全部数据", "类别=Furniture"}
    assert cards["v1"].data.rows == cards["p1"].data.rows


def test_amount_and_share_use_one_card_with_explicit_measure_choice():
    cards = {"v1": _card("v1", "Profit", "Category", ViewType.comparison),
             "v2": _card("v2", "Profit", "Category", ViewType.breakdown)}
    cards["v2"].chart_spec.type = ChartType.share_bar
    groups = _build_collapse_groups(cards)
    assert len(groups) == 1
    assert {m.measure_label for m in groups[0].members} == {"金额", "占比"}
