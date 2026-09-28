"""MetricSpec 确定性规则推断（P0 T03）。

原则（执行清单 T03）：
- 字段名只产出**候选**口径，规则保持显式、可审计；
- 非可加指标（率/评分/期末库存）默认均值/期末快照，绝不静默求和；
- 比率必须记录分母与「未按分母加权」限制；
- 利润等允许业务负值，库存类不允许但也不得仅凭分布删数（T02）。

用户显式确认的 FieldProfile.metric_spec 优先级高于本推断。
"""
from __future__ import annotations

import re

from app.schemas.dictionary import FieldProfile, MetricSpec

# 期末快照类：跨时间不可加（每天库存求和没有业务意义）
_STOCK_RE = re.compile(
    r"(ending[_ ]?stock|期末库存|库存余额|结存|on[_ ]?hand|stock[_ ]?on[_ ]?hand|"
    r"inventory[_ ]?(level|balance|qty|quantity)|月末库存|库存数量)",
    re.I,
)
# 评分/满意度类：均值展示，不可加
_SCORE_RE = re.compile(
    r"(satisfaction|满意度|csat|nps|评分|score|rating|星级|得分|好评率)", re.I
)
# 率/比率类：列内比率均值仅展示，未按分母加权；份额分析禁止
_RATE_RE = re.compile(
    r"(conversion[_ ]?rate|转化率|点击率|到达率|留存率| churn|"
    r"profit[_ ]?margin|利润率|毛利率|净利率|折扣率|discount[_ ]?rate|"
    r"率$|比率|rate$|ratio$|占比$|percent|pct$)",
    re.I,
)
# 业务上允许负值的指标
_ALLOW_NEG_RE = re.compile(
    r"(利润|profit|净利|毛利|亏损|结余|损益|net[_ ]?(income|profit)|ebitda|pst)", re.I
)
# 去重计数类（需要业务键，不能把行数称订单数）
_DISTINCT_RE = re.compile(
    r"(订单数|order[_ ]?(count|cnt|num)|订单量|独立客户|unique[_ ]?(user|customer)|客单)",
    re.I,
)
# 可加总量类
_SUM_RE = re.compile(
    r"(销量|销售额|销售收入|销售额|收入|营收|金额|总额|数量|件数|qty|quantity|"
    r"sales|revenue|amount|gmv|spend|花费|费用|成本|cost)",
    re.I,
)

_UNWEIGHTED_NOTE = (
    "该字段为列内比率/率值：分组均值仅作展示口径，未按各自分母加权，"
    "不参与占比/份额分析；如需精确口径请提供分子、分母字段。"
)


def infer_metric_spec(field: FieldProfile) -> MetricSpec | None:
    """由字段名 + 物理类型推断候选 MetricSpec；非指标字段返回 None。"""
    if not field.is_metric and field.semantic_type.value != "metric":
        return None
    name = field.name or ""
    unit = field.unit
    allow_neg = bool(_ALLOW_NEG_RE.search(name))

    if _STOCK_RE.search(name):
        return MetricSpec(
            aggregation="snapshot_last", additive=False,
            grain="entity_period_snapshot", time_role="snapshot",
            unit=unit, direction="neutral", allowed_negative=False,
            note="库存为每实体期末快照：跨实体期末可汇总，跨时间求和无业务意义。",
        )
    if _SCORE_RE.search(name):
        return MetricSpec(
            aggregation="mean", additive=False, grain="row",
            unit=unit, direction="higher_better",
            note="评分类指标取均值展示，不可加总，不参与份额分析。",
        )
    if _RATE_RE.search(name):
        return MetricSpec(
            aggregation="mean", additive=False, grain="row",
            unit=unit or "%", direction="higher_better",
            weighted=False, note=_UNWEIGHTED_NOTE,
        )
    if _DISTINCT_RE.search(name):
        return MetricSpec(
            aggregation="count_distinct", additive=False,
            grain="business_key", entity_key=None,
            unit=unit, direction="neutral",
            note="计数依赖业务键去重；未确认键字段前不把行数称为订单数。",
        )
    # 其余数值指标按可加总量处理（历史默认口径）
    return MetricSpec(
        aggregation="sum", additive=True, grain="row",
        unit=unit,
        direction="lower_better" if re.search(r"成本|cost|费用|花费", name, re.I)
        else "neutral",
        allowed_negative=allow_neg,
        note=("业务允许负值（亏损等），清洗与展示都不得按错误值删除。"
              if allow_neg else ""),
    )


def effective_metric_spec(field: FieldProfile) -> MetricSpec | None:
    """用户确认优先，其次规则推断。"""
    return field.metric_spec or infer_metric_spec(field)
