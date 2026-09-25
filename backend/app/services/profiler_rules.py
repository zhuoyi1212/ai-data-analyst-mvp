"""Semantic Profiler 的确定性规则层（不调用 LLM）。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.schemas.common import SemanticType
from app.services.parser import friendly_dtype, json_safe

_DATE_NAME = re.compile(r"(日期|时间|日$|date|time|day|created|updated)", re.IGNORECASE)
_ID_NAME = re.compile(
    r"(^id$|编号|订单号|工号|单号|票号|sku|编码|(^|[\s_-])id$|"
    r"(^|[\s_-])code$|[_-]?no$|ticket_?id)",
    re.IGNORECASE,
)
_METRIC_NAME = re.compile(
    r"(金额|总额|额$|价格|单价|数量|收入|营收|成本|花费|费用|库存|时长|满意度|"
    r"率$|人数|次数|天数|小时|销售额|销量|出库|入库|"
    r"amount|price|qty|quantity|revenue|sales|spend|cost|stock|count|"
    r"impressions|clicks|conversions|sessions|dau|users|tickets|"
    r"satisfaction|duration|hours|avg|total)",
    re.IGNORECASE,
)
_GEO_NAME = re.compile(r"(区域|地区|省份?|城市|国家|地址|region|city|province|country|area)", re.IGNORECASE)

_ENUM_CARD = 30  # 低基数字符串视为枚举维度
_HIGH_CARD_RATIO = 0.5  # 高基数占比超过行数一半，疑似自由文本/标识


@dataclass
class RuleProfile:
    name: str
    physical_type: str
    examples: list[Any] = field(default_factory=list)
    null_rate: float = 0.0
    cardinality: int = 0
    stats: dict[str, Any] = field(default_factory=dict)
    guess: SemanticType | None = None
    locked: SemanticType | None = None  # 规则证据充分，锁定语义
    hints: list[str] = field(default_factory=list)
    date_parse_ratio: float = 0.0


def _date_parse_ratio(series: pd.Series) -> float:
    sample = series.dropna().astype(str).head(100)
    if sample.empty:
        return 0.0
    parsed = pd.to_datetime(sample, errors="coerce", format="mixed")
    return float(parsed.notna().mean())


def profile_column(df: pd.DataFrame, name: str) -> RuleProfile:
    s = df[name]
    physical = friendly_dtype(s)
    n = len(s)
    nulls = int(s.isna().sum())
    examples = [json_safe(v) for v in s.dropna().head(5).tolist()]
    cardinality = int(s.nunique(dropna=True))
    rp = RuleProfile(
        name=name,
        physical_type=physical,
        examples=examples,
        null_rate=round(nulls / n, 4) if n else 0.0,
        cardinality=cardinality,
    )

    if physical in {"integer", "float"}:
        desc = s.describe()
        rp.stats = {
            "min": json_safe(desc.get("min")),
            "max": json_safe(desc.get("max")),
            "mean": json_safe(desc.get("mean")),
        }
    elif physical == "datetime":
        rp.locked = SemanticType.date
        rp.hints.append("物理类型为日期时间")
    elif physical == "boolean":
        rp.guess = SemanticType.dimension
        rp.hints.append("布尔枚举字段")
    else:  # string
        # 日期可解析性
        ratio = _date_parse_ratio(s)
        rp.date_parse_ratio = ratio
        if ratio >= 0.9:
            rp.hints.append(f"{int(ratio*100)}% 文本可解析为日期")
        # 值长度样例
        rp.stats["sample_lengths"] = [len(str(v)) for v in s.dropna().head(5).tolist()]

    # 命名模式（对所有类型生效，用于锁定 id / 日期）
    if rp.locked is None:
        if _ID_NAME.search(name) and (
            physical == "integer" or cardinality >= max(1, int(n * _HIGH_CARD_RATIO))
        ):
            rp.locked = SemanticType.id
            rp.hints.append("命名符合标识字段且取值高度离散")
        elif physical == "datetime" or (
            _DATE_NAME.search(name) and rp.date_parse_ratio >= 0.6
        ):
            rp.locked = SemanticType.date
            rp.hints.append("命名与取值均符合日期字段")
        elif rp.date_parse_ratio >= 0.95:
            rp.locked = SemanticType.date
            rp.hints.append("取值几乎全部可解析为日期")

    # 非锁定的类型猜测
    if rp.locked is None and rp.guess is None:
        if physical in {"integer", "float"}:
            if _METRIC_NAME.search(name):
                rp.guess = SemanticType.metric
                rp.hints.append("数值型且命名符合指标特征")
            else:
                rp.guess = SemanticType.metric  # 数值默认指标候选，交模型补义
        elif physical == "string":
            if _GEO_NAME.search(name):
                rp.guess = SemanticType.geo
                rp.hints.append("命名符合地理维度")
            elif cardinality <= _ENUM_CARD:
                rp.guess = SemanticType.dimension
                rp.hints.append(f"低基数文本（{cardinality} 种取值），疑似枚举维度")
            elif _METRIC_NAME.search(name):
                rp.guess = SemanticType.metric
            else:
                rp.hints.append("高基数文本，语义不明确")
                rp.guess = SemanticType.unknown

    return rp


def rule_profile_all(df: pd.DataFrame) -> dict[str, RuleProfile]:
    return {name: profile_column(df, name) for name in df.columns}


def detect_relations(
    df: pd.DataFrame, metric_columns: list[str]
) -> list[dict[str, Any]]:
    """确定性探测：乘积关系（单价×数量≈金额）与高相关数值列对。"""
    relations: list[dict[str, Any]] = []
    nums = [c for c in metric_columns if c in df.columns]
    numeric = df[nums].apply(pd.to_numeric, errors="coerce") if nums else pd.DataFrame()

    # 乘积三元组 a × b ≈ c
    for i, a in enumerate(nums):
        for b in nums[i + 1 :]:
            prod = numeric[a] * numeric[b]
            for c in nums:
                if c in {a, b}:
                    continue
                valid = prod.notna() & numeric[c].notna() & (numeric[c].abs() > 1e-9)
                if valid.sum() < 50:
                    continue
                rel_err = ((prod[valid] - numeric[c][valid]).abs() / numeric[c][valid].abs())
                if float(rel_err.median()) < 0.02:
                    relations.append(
                        {
                            "type": "product",
                            "columns": [a, b, c],
                            "note": f"约 98% 以上行满足「{a} × {b} ≈ {c}」",
                        }
                    )

    # 高相关列对
    pairs = []
    for i, a in enumerate(nums):
        for b in nums[i + 1 :]:
            valid = numeric[[a, b]].dropna()
            if len(valid) < 30 or valid[a].std() == 0 or valid[b].std() == 0:
                continue
            r = float(valid[a].corr(valid[b]))
            if abs(r) >= 0.75:
                pairs.append((abs(r), a, b, round(r, 3)))
    for _, a, b, r in sorted(pairs, reverse=True)[:5]:
        relations.append(
            {
                "type": "correlation",
                "columns": [a, b],
                "coefficient": r,
                "note": f"「{a}」与「{b}」相关系数 {r}（相关关系，非因果）",
            }
        )
    return relations
