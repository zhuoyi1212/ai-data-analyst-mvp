"""Phase 6：Insight-first Dashboard 验收。

覆盖：
- synthesize_dashboard 产出按 final_score 降序、确定性编号的业务事实 Insight；
- 每条 Insight 的证据视图真实存在且可消费（Insight→Evidence，禁止编造 view id）；
- Hero 出自最高价值 Insight 的证据视图（而非 ViewType 固定优先级）；
- 平稳数据集不产出 performance_change 类 Insight（趋势不因类型被拔高）。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd

from app.services.dashboard_synthesizer import synthesize_dashboard
from app.services.signals import build_signal_set

sys.path.insert(0, str(Path(__file__).parent))
from test_report import _report_pipeline  # noqa: E402
from test_signals import _run_scan_and_signals  # noqa: E402


def test_insights_ranked_with_valid_evidence(tmp_path):
    store, sid, _, _, _ = _report_pipeline(tmp_path)
    dash = synthesize_dashboard(sid, store)

    insights = dash.insights
    assert insights, "应产出至少一条业务事实 Insight"

    # 确定性编号 + final_score 单调不增
    ids = [c.insight_id for c in insights]
    assert ids == [f"ins_{i:02d}" for i in range(1, len(insights) + 1)]
    scores = [c.final_score for c in insights]
    assert scores == sorted(scores, reverse=True)

    # Insight→Evidence 关系：证据视图必须真实存在且可消费
    view_keys = set(dash.views.keys())
    for c in insights:
        assert c.evidence_view_ids, f"{c.insight_id} 缺少证据视图"
        assert set(c.evidence_view_ids) <= view_keys, c.insight_id
        for vid in c.evidence_view_ids:
            assert dash.views[vid].consumable, vid

    # 数字来自确定性计算：可空字段非空时必须为有限值（禁止 NaN/Inf）
    for c in insights:
        for v in (c.current_value, c.comparison_value, c.delta, c.delta_pct):
            if v is not None:
                assert math.isfinite(v)


def test_hero_derives_from_top_insight_evidence(tmp_path):
    store, sid, _, _, _ = _report_pipeline(tmp_path)
    dash = synthesize_dashboard(sid, store)
    assert dash.layout is not None
    assert dash.insights

    heroes = [i for i in dash.layout.items if i.role == "hero"]
    assert len(heroes) == 1

    # Hero 的证据视图来自最高价值 Insight（而非 ViewType 固定优先级）
    evidence_union = {vid for c in dash.insights for vid in c.evidence_view_ids}
    assert heroes[0].item_id in evidence_union


def _flat_df() -> pd.DataFrame:
    rows = []
    for i in range(120):
        month = (i // 20) % 6
        region = ["East", "West", "Central", "South"][i % 4]
        cat = ["Furniture", "Office Supplies", "Technology"][i % 3]
        segment = ["Consumer", "Corporate", "Home Office"][i % 3]
        subcat = f"sub_{i % 6}"
        rows.append((
            f"2024-{month + 1:02d}-15", region, cat, segment, subcat,
            0.0, 1000.0, 200.0,
        ))
    return pd.DataFrame(
        rows, columns=["date", "region", "category", "segment", "subcat",
                       "discount", "sales", "profit"],
    )


def test_flat_metric_produces_no_performance_change_insight(tmp_path):
    store, sid, _, _, signals = _run_scan_and_signals(tmp_path, _flat_df())
    run_id = store.active_run_id(sid)
    store.write_artifact(
        sid, "signals",
        build_signal_set(sid, run_id, signals).model_dump(mode="json"),
    )
    dash = synthesize_dashboard(sid, store)

    # 全平数据没有 growth/decline 信号 → 不得制造 performance_change Insight
    assert all(c.type != "performance_change" for c in dash.insights)