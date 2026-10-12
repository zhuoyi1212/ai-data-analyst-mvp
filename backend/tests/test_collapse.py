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
