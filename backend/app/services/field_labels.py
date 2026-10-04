"""字段中文名映射（纯确定性，无 LLM）。

图表/表格向中文读者展示时，用 field_label() 把英文物理列名翻译为中文业务名：
1. 常见业务词内置词典（整名/分词命中）；
2. 数据字典 FieldProfile.meaning（profiler 推断的中文含义，简洁时采用）；
3. 都不命中则保留原名（绝不硬译）。

humanize_text() 用于把标题/问题中夹杂的英文业务词替换为中文。
"""
from __future__ import annotations

import re

# 常见英文业务词 → 中文（按最长词优先匹配；小写键）
_WORDS: dict[str, str] = {
    # Superstore / 零售
    "order_date": "订单日期", "ship_date": "发货日期",
    "order_id": "订单编号", "ship_mode": "配送方式",
    "customer_id": "客户编号", "customer_name": "客户姓名",
    "product_id": "产品编号", "product_name": "产品名称",
    "sub-category": "子类别", "subcategory": "子类别", "subcat": "子类别",
    "postal_code": "邮政编码",
    "sales": "销售额", "revenue": "营业收入", "profit": "利润",
    "profitability": "利润率", "discount": "折扣", "quantity": "销售数量",
    "cost": "成本", "price": "单价", "amount": "金额",
    "gross_sales": "总销售额", "net_sales": "净销售额",
    "gross_profit": "毛利", "net_profit": "净利润",
    "margin": "利润率", "profit_margin": "利润率",
    "mode": "方式", "row": "行", "id": "编号",
    "order": "订单", "ship": "配送",
    # 维度
    "region": "区域", "category": "类别", "segment": "客户细分",
    "country": "国家", "city": "城市", "state": "省/州",
    "market": "市场", "channel": "渠道", "department": "部门",
    "supplier": "供应商", "brand": "品牌", "store": "门店",
    "product": "产品", "customer": "客户", "employee": "员工",
    # 通用
    "date": "日期", "time": "时间", "year": "年份", "month": "月份",
    "week": "周", "quarter": "季度", "day": "日",
    "name": "名称", "type": "类型", "status": "状态",
    "description": "描述", "label": "标签", "code": "编码",
    "age": "年龄", "gender": "性别", "address": "地址",
    "email": "邮箱", "phone": "电话",
    "total": "合计", "average": "平均值", "avg": "平均值",
    "sum": "总和", "count": "计数", "number": "数量",
    "score": "评分", "rating": "评级", "level": "等级",
    "start": "开始", "end": "结束", "duration": "持续时长",
    "value": "数值", "share": "占比", "ratio": "比率", "rate": "比率",
    "percent": "百分比", "pct": "百分比", "percentage": "百分比",
    "growth": "增长率", "change": "变化量", "delta": "变化量",
    "contribution": "贡献量",
    "budget": "预算", "actual": "实际值", "forecast": "预测值",
    "is_outlier": "离群标记", "outlier_score": "离群程度",
}

# 输出规范列（value/share）需要指标上下文，由调用方显式给名
_SPLIT = re.compile(r"[\s_/\-]+")


def _lookup(phrase: str) -> str | None:
    key = phrase.strip().lower()
    if key in _WORDS:
        return _WORDS[key]
    parts = [p for p in _SPLIT.split(key) if p]
    if len(parts) > 1:
        translated = [_WORDS.get(p) for p in parts]
        if all(translated):
            return "".join(translated)
    return None


def field_label(
    name: str,
    *,
    meaning: str | None = None,
    metric_context: str | None = None,
) -> str:
    """物理列名 → 中文展示名。

    metric_context：规范输出列 value 的指标名（如 sales），此时 value→「销售额」。
    """
    if name == "value" and metric_context:
        base = _lookup(metric_context)
        return base or metric_context
    if name == "share" and metric_context:
        base = _lookup(metric_context)
        return f"{base or metric_context}占比"
    hit = _lookup(name)
    if hit:
        return hit
    if meaning:
        text = meaning.strip()
        # profiler 的中文含义：短且含中文即采用
        if len(text) <= 12 and re.search(r"[\u4e00-\u9fff]", text):
            return text
    return name


# 标题中英文词替换：按词典 key 长度降序，词边界匹配，避免短词污染
_REPLACEMENT = sorted(
    ((k, v) for k, v in _WORDS.items() if k.isalpha() and len(k) >= 3),
    key=lambda kv: -len(kv[0]),
)


def humanize_text(text: str) -> str:
    """把中文标题/问题中夹杂的英文业务词替换为中文（如 region→区域）。"""
    def repl(m: re.Match) -> str:
        return _WORDS[m.group(0).lower()]

    for en, _zh in _REPLACEMENT:
        text = re.sub(rf"(?<![A-Za-z]){re.escape(en)}(?![A-Za-z])", repl, text,
                      flags=re.IGNORECASE)
    return text
