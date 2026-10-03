"""Task 2：Broad Scan 广度扫描器测试。

覆盖验收：
- TR-2.1 字段完整数据上支持类别全部产出（含 Top/Bottom 对称）；
        无日期/无派生条件时对应类别缺席，数量自适应；
- TR-2.2 全部 scan 视图经既有 execute_bundle 门禁成功且可消费；
        五个示例集均无失败视图，一键链路改用 scan 后保持通过。
"""
from __future__ import annotations

from collections import Counter

from app.schemas.bundle import AnalysisBundle, ViewType
from app.services.broad_scan import _CATEGORY_OF_TYPE, plan_scan
from app.services.bundle_executor import execute_bundle
from test_bundle_planner import _ready_session


def _categories(bundle: AnalysisBundle) -> Counter:
    return Counter(
        _CATEGORY_OF_TYPE[v.type]
        for v in bundle.analysis_views
    )


# ------------------------------------------------------------ TR-2.1 覆盖


def test_scan_full_field_data_covers_supported_categories(tmp_path):
    store, sid, dictionary, snapshot = _ready_session("sales_orders", tmp_path)

    bundle = plan_scan(dictionary, snapshot, title="scan")
    cats = _categories(bundle)

    # 数据支持的类别全部产出
    assert cats["kpi"] >= 1
    assert cats["trend"] >= 1
    assert cats["comparison"] >= 1
    assert cats["top_bottom"] >= 2  # Top/Bottom 对称
    assert cats["share"] >= 1
    assert cats["anomaly"] >= 1
    assert cats["relationship"] == 1
    # sales_orders 无利润字段 → 盈利效率类别自适应缺席
    assert cats["profitability"] == 0

    # 总量受封顶；view_id 顺序唯一；信息层级从 KPI 开始
    assert len(bundle.analysis_views) <= 24
    assert bundle.analysis_views[0].type is ViewType.overview
    assert [v.view_id for v in bundle.analysis_views] == [
        f"view_{i:02d}" for i in range(1, len(bundle.analysis_views) + 1)
    ]
    # 全部来源标记为广扫
    assert all(v.source == "rule.broad_scan" for v in bundle.analysis_views)


def test_scan_top_bottom_are_symmetric(tmp_path):
    store, sid, dictionary, snapshot = _ready_session("sales_orders", tmp_path)

    bundle = plan_scan(dictionary, snapshot)
    ranking = [v for v in bundle.analysis_views if v.type is ViewType.ranking]
    orders = [v.plan.steps[0].params.order.value for v in ranking]

    assert "desc" in orders and "asc" in orders
    # 成对出现：每个 Top 都有对应 Bottom
    assert orders.count("desc") == orders.count("asc")
    # Bottom 视图标题/问题明示「最低」
    bottom = next(v for v in ranking if v.plan.steps[0].params.order.value == "asc")
    assert "Bottom" in bottom.title


def test_scan_trend_absent_without_date(tmp_path):
    store, sid, dictionary, snapshot = _ready_session("sales_orders", tmp_path)
    # 剥离日期字段：趋势类别应缺席
    no_date = dictionary.model_copy(update={
        "fields": [f for f in dictionary.fields if f.semantic_type.value != "date"]
    })

    bundle = plan_scan(no_date, snapshot)
    cats = _categories(bundle)

    assert cats["trend"] == 0
    # 其他类别不受影响
    assert cats["kpi"] >= 1
    assert cats["relationship"] == 1


def test_scan_adapts_to_field_conditions_all_samples(tmp_path):
    """五个示例集：类别集合只包含其字段真正支持的类别。"""
    from scripts.generate_sample_data import DATASETS_CONFIG

    for name in DATASETS_CONFIG:
        store, sid, dictionary, snapshot = _ready_session(name, tmp_path)
        bundle = plan_scan(dictionary, snapshot)
        cats = _categories(bundle)

        has_date = bool(dictionary.date_fields())
        assert (cats["trend"] > 0) is has_date, name
        # 至少有 KPI 与一类结构视角
        assert cats["kpi"] >= 1, name
        assert len(bundle.analysis_views) >= 4, name


# ------------------------------------------------------------ TR-2.2 执行门禁


def test_scan_views_execute_with_checks(tmp_path):
    store, sid, dictionary, snapshot = _ready_session("sales_orders", tmp_path)

    bundle = plan_scan(dictionary, snapshot)
    store.write_bundle(sid, bundle.model_dump(mode="json"))
    result = execute_bundle(sid, store, bundle)

    assert result.failed == 0
    assert result.succeeded == len(bundle.analysis_views)
    for view_result in result.views:
        assert view_result.status == "success"
        assert view_result.result_rows_total > 0
        assert view_result.checks  # 既有校验门禁全部执行


def test_scan_executes_clean_on_all_samples(tmp_path):
    """五个示例集广扫视图批量执行零失败。"""
    from scripts.generate_sample_data import DATASETS_CONFIG

    for name in DATASETS_CONFIG:
        store, sid, dictionary, snapshot = _ready_session(name, tmp_path)
        bundle = plan_scan(dictionary, snapshot)
        store.write_bundle(sid, bundle.model_dump(mode="json"))
        from app.services.bundle_executor import execute_bundle

        result = execute_bundle(sid, store, bundle)
        assert result.failed == 0, (
            name, [v for v in result.views if v.status != "success"]
        )
