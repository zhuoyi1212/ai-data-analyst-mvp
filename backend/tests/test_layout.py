"""Task 7：Tableau 风格 12-column Dashboard Composition 测试。

覆盖：
- TR-7.1 各 role 就位、贪心换行每行跨度 ≤12、视图不重复放置、可见项可消费；
- TR-7.2 同输入确定性、rationale 非空、折叠项理由一致；
- TR-7.3 refine 筛选重算后布局仍合法。
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.schemas.bundle import AnalysisBundle
from app.schemas.dashboard import DashboardArtifact
from app.services.bundle_executor import execute_bundle
from app.services.bundle_planner import refine_bundle
from app.services.dashboard_synthesizer import synthesize_dashboard
from app.services.layout_composer import compose_layout

sys.path.insert(0, str(Path(__file__).parent))
from test_report import _report_pipeline  # noqa: E402


def _assert_rows(items) -> None:
    used = 0
    for item in items:
        if used + item.col_span > 12:
            used = 0
        used += item.col_span
        assert used <= 12


# ------------------------------------------------------------ TR-7.1


def test_layout_roles_and_grid(tmp_path):
    store, sid, run_id, graph, report = _report_pipeline(tmp_path)
    dash = synthesize_dashboard(sid, store)
    assert dash.layout is not None
    items = dash.layout.items

    # 六个 role 在 Superstore 数据下均就位
    roles = {item.role for item in items}
    assert roles == {
        "kpi", "hero", "primary", "supporting", "diagnostic", "findings"
    }

    # 无重复放置
    ids = [item.item_id for item in items]
    assert len(ids) == len(set(ids))

    # 每行跨度 ≤12（贪心换行）
    _assert_rows(items)

    # 可见的 view/probe 项必须对应可消费卡片
    for item in items:
        if item.ref_type == "meta" or item.default_hidden:
            continue
        card = dash.views[item.item_id]
        assert card.consumable
        assert item.ref_type == card.ref_type


# ------------------------------------------------------------ TR-7.2


def test_layout_deterministic_and_rationales(tmp_path):
    store, sid, run_id, graph, report = _report_pipeline(tmp_path)
    dash = synthesize_dashboard(sid, store)

    again = compose_layout(
        kpis=dash.kpis, cards=dash.views, findings=dash.findings,
        chain_set=None,  # 无 chains 也须确定性
    )
    once_more = compose_layout(
        kpis=dash.kpis, cards=dash.views, findings=dash.findings,
        chain_set=None,
    )
    assert (
        [i.model_dump() for i in again.items]
        == [i.model_dump() for i in once_more.items]
    )

    for item in dash.layout.items:
        assert item.rationale.strip()

    # 折叠项确实存在且理由含折叠/低优先级语义
    hidden = [i for i in dash.layout.items if i.default_hidden]
    assert hidden
    assert any(
        ("折叠" in i.rationale or "较低" in i.rationale) for i in hidden
    )


# ------------------------------------------------------------ TR-7.3


def test_layout_valid_after_refine(tmp_path):
    store, sid, run_id, graph, report = _report_pipeline(tmp_path)
    before = synthesize_dashboard(sid, store)

    f = next(x for x in before.global_filters if x.members)
    member = f.members[0]
    snapshot = store.load_snapshot(sid)
    source = AnalysisBundle.model_validate(store.read_bundle(sid))
    scope = [{"column": f.column, "values": [member]}]
    new_bundle = refine_bundle(source, snapshot, scope)
    store.write_bundle(
        sid, new_bundle.model_dump(mode="json"), scope_filters=scope
    )
    execute_bundle(sid, store, new_bundle)
    after = synthesize_dashboard(sid, store)

    assert after.layout is not None
    _assert_rows(after.layout.items)
    ids = [i.item_id for i in after.layout.items]
    assert len(ids) == len(set(ids))
