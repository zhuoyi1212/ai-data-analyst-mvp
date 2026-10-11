"""MetricSpec 规则推断单元测试：单价/价格归为率类（均值、不可加）。"""
from __future__ import annotations

from app.schemas.common import Confidence, SemanticType
from app.schemas.dictionary import FieldProfile
from app.services.metric_spec import infer_metric_spec


def _metric(name: str) -> FieldProfile:
    return FieldProfile(
        name=name, physical_type="float", semantic_type=SemanticType.metric,
        meaning=name, confidence=Confidence.high, confirmed=True,
        cardinality=100, is_metric=True,
    )


def test_unit_price_is_rate_like():
    spec = infer_metric_spec(_metric("单价"))
    assert spec is not None
    assert spec.aggregation == "mean"
    assert spec.additive is False


def test_amount_is_additive_sum():
    spec = infer_metric_spec(_metric("金额"))
    assert spec is not None
    assert spec.aggregation == "sum"
    assert spec.additive is True


def test_quantity_is_additive_sum():
    spec = infer_metric_spec(_metric("数量"))
    assert spec is not None
    assert spec.aggregation == "sum"
    assert spec.additive is True
