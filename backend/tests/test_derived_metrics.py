"""Phase 2：确定性派生指标（利润率）规则探测测试。LLM 不参与定义与计算。"""
from __future__ import annotations

from app.schemas.common import Confidence, SemanticType
from app.schemas.dictionary import DataDictionary, FieldProfile
from app.services.derived_metrics import detect_derived_metrics


def _metric(name: str) -> FieldProfile:
    return FieldProfile(
        name=name, physical_type="float", semantic_type=SemanticType.metric,
        meaning=name, confidence=Confidence.high, confirmed=True,
        cardinality=100, is_metric=True,
    )


def _dim(name: str, card: int = 3) -> FieldProfile:
    return FieldProfile(
        name=name, physical_type="string", semantic_type=SemanticType.dimension,
        meaning=name, confidence=Confidence.high, confirmed=True, cardinality=card,
    )


def _dict(*names: str) -> DataDictionary:
    return DataDictionary(session_id="s", fields=[_metric(n) for n in names], complete=True)


def test_superstore_naming_yields_profit_margin():
    specs = detect_derived_metrics(_dict("Sales", "Quantity", "Discount", "Profit"))
    assert len(specs) == 1
    s = specs[0]
    assert s.key == "profit_margin"
    assert s.numerator == "Profit"
    assert s.denominator == "Sales"
    assert s.agg == "ratio_of_sums"
    assert s.source == "rule.derived"


def test_chinese_naming_and_denominator_specificity():
    # 同时存在「销售额」与泛化「金额」时，优先用更明确的销售额
    specs = detect_derived_metrics(_dict("利润", "销售额", "金额"))
    assert specs[0].numerator == "利润"
    assert specs[0].denominator == "销售额"


def test_net_profit_preferred_over_generic_profit():
    specs = detect_derived_metrics(_dict("毛利", "净利润", "营业收入"))
    assert specs[0].numerator == "净利润"
    assert specs[0].denominator == "营业收入"


def test_no_profit_field_no_spec():
    assert detect_derived_metrics(_dict("Sales", "Quantity", "Discount")) == []


def test_no_revenue_field_no_spec():
    assert detect_derived_metrics(_dict("Profit", "Quantity")) == []


def test_rate_named_field_is_not_composed_with_itself():
    # 「Profit Margin」是率本身，不能拿它当分子
    specs = detect_derived_metrics(_dict("Profit Margin", "Sales"))
    assert specs == []


def test_dimensions_are_ignored():
    d = DataDictionary(session_id="s", fields=[_metric("Profit"), _metric("Sales"),
                                              _dim("品类")], complete=True)
    specs = detect_derived_metrics(d)
    assert len(specs) == 1
    assert (specs[0].numerator, specs[0].denominator) == ("Profit", "Sales")
