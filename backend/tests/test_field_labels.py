"""字段中文化与图表解读文案的单元测试（纯确定性）。"""
import pandas as pd

from app.services.card_interpretation import interpret_view
from app.services.field_labels import field_label, humanize_text


# --------------------------------------------------------------- field_label

def test_field_label_builtin_words():
    assert field_label("sales") == "销售额"
    assert field_label("profit") == "利润"
    assert field_label("discount") == "折扣"
    assert field_label("region") == "区域"
    assert field_label("category") == "类别"
    assert field_label("subcat") == "子类别"
    assert field_label("segment") == "客户细分"


def test_field_label_value_with_metric_context():
    # 结果列 value/share 本身无含义，需锚定指标
    assert field_label("value", metric_context="sales") == "销售额"
    assert field_label("share", metric_context="sales") == "销售额占比"
    assert field_label("value") == "数值"


def test_field_label_composite():
    # 下划线分词后逐词拼接
    assert field_label("region_category") == "区域类别"


def test_field_label_meaning_fallback():
    # 词典未命中时采用数据字典 meaning（需含中文）
    assert field_label("foo_bar", meaning="自定义业务含义") == "自定义业务含义"
    # 英文 meaning 不采用，保留原名
    assert field_label("foo_bar", meaning="some english") == "foo_bar"
    # 完全未知：保留原名
    assert field_label("zzz_unknown_col") == "zzz_unknown_col"


# -------------------------------------------------------------- humanize_text

def test_humanize_text_replaces_english_words():
    out = humanize_text("Sales by Region")
    assert "销售额" in out
    assert "区域" in out
    assert "Sales" not in out


def test_humanize_text_preserves_chinese():
    assert "销售额" in humanize_text("销售额 Trend")


# ------------------------------------------------------------- interpret_view

def test_interpret_breakdown_with_share():
    df = pd.DataFrame({
        "region": ["华东", "华北", "华南"],
        "value": [600.0, 300.0, 100.0],
        "share": [0.6, 0.3, 0.1],
    })
    text = interpret_view(
        view_type="breakdown", df=df,
        metric_fields=["sales"], dimension_fields=["region"],
    )
    assert "构成占比" in text
    assert "华东" in text
    assert "60%" in text


def test_interpret_comparison_extremes():
    df = pd.DataFrame({
        "category": ["A", "B", "C"],
        "value": [100.0, 200.0, 50.0],
    })
    text = interpret_view(
        view_type="comparison", df=df,
        metric_fields=["profit"], dimension_fields=["category"],
    )
    assert "B 最高" in text
    assert "C 最低" in text


def test_interpret_ranking():
    df = pd.DataFrame({"subcat": ["X", "Y"], "value": [9.0, 3.0]})
    text = interpret_view(
        view_type="ranking", df=df,
        metric_fields=["sales"], dimension_fields=["subcat"],
    )
    assert "排名" in text
    assert "首位 X" in text


def test_interpret_single_group_guard():
    df = pd.DataFrame({"region": ["华东"], "value": [42.0]})
    text = interpret_view(
        view_type="comparison", df=df,
        metric_fields=["sales"], dimension_fields=["region"],
    )
    assert "仅含一个分组" in text
    assert "42" in text


def test_interpret_trend():
    df = pd.DataFrame({
        "date": ["2024-01", "2024-02", "2024-03"],
        "value": [10.0, 30.0, 20.0],
    })
    text = interpret_view(
        view_type="trend", df=df,
        metric_fields=["sales"], dimension_fields=[],
    )
    assert "趋势" in text
    assert "峰值" in text
    assert "2024-02" in text


def test_interpret_overview():
    df = pd.DataFrame({"value": [1234.0]})
    text = interpret_view(
        view_type="overview", df=df, metric_fields=["sales"],
    )
    assert "1234" in text
    assert "总量" in text


def test_interpret_anomaly():
    df = pd.DataFrame({
        "date": ["d1", "d2"], "region": ["r1", "r2"],
        "is_outlier": [True, True],
    })
    text = interpret_view(view_type="anomaly", df=df)
    assert "共 2 条" in text


def test_interpret_relationship():
    df = pd.DataFrame({"discount": [0.1, 0.2], "profit": [10.0, 5.0]})
    text = interpret_view(view_type="relationship", df=df)
    assert "相关" in text
    assert "因果" in text


def test_interpret_empty_df_safe():
    assert interpret_view(
        view_type="trend", df=pd.DataFrame(), metric_fields=["sales"],
    ) == ""
