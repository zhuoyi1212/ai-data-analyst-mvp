"""确定性派生指标注册表与字段探测（重构 Phase 2）。

红线：
- 比率的「定义」只能由本模块的规则模板给出（哪些字段相除、叫什么）；
- 比率的「数值」只能由 engine.ops.op_derive_ratio 在快照上计算；
- LLM 既不决定公式也不计算结果。未来新增模板（毛利率/ROI/客单价）只在此扩展。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas.derived_metric import DerivedMetricSpec
from app.schemas.dictionary import DataDictionary, FieldProfile
from app.schemas.common import SemanticType


@dataclass(frozen=True)
class RatioTemplate:
    key: str
    label: str
    numerator: re.Pattern
    denominator: re.Pattern
    unit: str = "%"


# 本身就是率/折扣的字段名不能当分子或分母（不能用「利润率」算「利润率」）
_RATE_NAME = re.compile(r"(率|比率|rate|ratio|margin|占比|percent|%|折扣|discount)", re.I)

# 模板顺序即优先级（未来可追加 gross_margin、ROI 等）
_TEMPLATES: list[RatioTemplate] = [
    RatioTemplate(
        key="profit_margin",
        label="利润率",
        numerator=re.compile(r"(净利润|净利|毛利润|毛利|利润|profit)", re.I),
        denominator=re.compile(
            r"(销售额|销售收入|销售金额|营收|营业收入|营业额|收入|"
            r"revenue|sales|turnover|amount|金额)",
            re.I,
        ),
    ),
]

# 分母候选内部的特异性排序：越明确是「销售/营收」越靠前，泛化的 amount/收入/金额 靠后
_DEN_SPEC = [
    re.compile(r"(销售额|销售收入|销售金额|revenue|sales|turnover)", re.I),
    re.compile(r"(营收|营业收入|营业额)", re.I),
    re.compile(r"(收入|amount|金额)", re.I),
]
# 分子候选特异性：净利润/净利 > 毛利 > 泛利润
_NUM_SPEC = [
    re.compile(r"(净利润|净利|net.?profit|net.?income)", re.I),
    re.compile(r"(毛利润|毛利|gross.?profit)", re.I),
    re.compile(r"(利润|profit)", re.I),
]


def _metric_fields(dictionary: DataDictionary) -> list[FieldProfile]:
    return [
        f for f in dictionary.fields
        if f.semantic_type is SemanticType.metric and not f.ignored
    ]


def _spec_rank(patterns: list[re.Pattern], name: str) -> int | None:
    for i, pat in enumerate(patterns):
        if pat.search(name):
            return i
    return None


def _detect_one(tpl: RatioTemplate, fields: list[FieldProfile]) -> DerivedMetricSpec | None:
    nums = [
        (rank, f) for f in fields
        if not _RATE_NAME.search(f.name)
        and (rank := _spec_rank(_NUM_SPEC, f.name)) is not None
        and tpl.numerator.search(f.name)
    ]
    dens = [
        (rank, f) for f in fields
        if not _RATE_NAME.search(f.name)
        and (rank := _spec_rank(_DEN_SPEC, f.name)) is not None
        and tpl.denominator.search(f.name)
    ]
    if not nums or not dens:
        return None
    nums.sort(key=lambda x: (x[0], x[1].name))
    dens.sort(key=lambda x: (x[0], x[1].name))
    num_field = nums[0][1]
    den_field = next((f for _, f in dens if f.name != num_field.name), None)
    if den_field is None:
        return None
    return DerivedMetricSpec(
        key=tpl.key, label=tpl.label,
        numerator=num_field.name, denominator=den_field.name,
        unit=tpl.unit,
    )


def detect_derived_metrics(dictionary: DataDictionary) -> list[DerivedMetricSpec]:
    """按注册表顺序探测可派生的比率指标；探测不到不产出（不强行造指标）。"""
    fields = _metric_fields(dictionary)
    out: list[DerivedMetricSpec] = []
    used_fields: set[str] = set()
    for tpl in _TEMPLATES:
        spec = _detect_one(tpl, fields)
        if spec and (spec.numerator, spec.denominator) not in used_fields:
            out.append(spec)
            used_fields.add((spec.numerator, spec.denominator))
    return out
