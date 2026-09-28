"""跨阶段共享的枚举与基础类型。"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class SemanticType(str, Enum):
    dimension = "dimension"  # 维度
    metric = "metric"  # 指标
    date = "date"  # 日期
    id = "id"  # 标识
    geo = "geo"  # 地理
    unknown = "unknown"  # 待确认


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class ChartType(str, Enum):
    metric = "metric"  # 指标卡
    metric_compare = "metric_compare"  # 指标卡+对比
    bar = "bar"  # 柱状/条形
    grouped_bar = "grouped_bar"  # 分组柱状
    share_bar = "share_bar"  # 占比条形
    pie = "pie"
    line = "line"  # 折线
    line_outlier = "line_outlier"  # 折线+离群标记
    scatter = "scatter"  # 散点
    table = "table"  # 明细表


class QuestionCategory(str, Enum):
    overview = "overview"  # 总量概览
    trend = "trend"  # 趋势
    comparison = "comparison"  # 对比
    share = "share"  # 占比
    ranking = "ranking"  # 排名
    anomaly = "anomaly"  # 异常
    correlation = "correlation"  # 相关


class InsightType(str, Enum):
    key_finding = "key_finding"
    trend = "trend"
    comparison = "comparison"
    anomaly = "anomaly"
    risk = "risk"


class CheckLevel(str, Enum):
    passed = "pass"
    warning = "warn"
    failed = "fail"


class AggFunc(str, Enum):
    sum = "sum"
    mean = "mean"
    median = "median"
    count = "count"
    count_distinct = "count_distinct"
    min = "min"
    max = "max"
    last = "last"  # 期末/末点快照值（库存等非跨期可加指标）


class SortOrder(str, Enum):
    desc = "desc"
    asc = "asc"


class TimeGranularity(str, Enum):
    day = "day"
    week = "week"
    month = "month"
    quarter = "quarter"
    year = "year"


class ComparePeriod(str, Enum):
    yoy = "yoy"  # 同比
    mom = "mom"  # 环比
    wow = "wow"  # 周环比


class CorrMethod(str, Enum):
    pearson = "pearson"
    spearman = "spearman"


class OutlierMethod(str, Enum):
    iqr = "iqr"
    zscore = "zscore"


class FilterOperator(str, Enum):
    eq = "=="
    ne = "!="
    gt = ">"
    ge = ">="
    lt = "<"
    le = "<="
    in_set = "in"
    not_in = "not_in"
    contains = "contains"
    between = "between"
    date_between = "date_between"


class FieldInfo(BaseModel):
    """供方案校验使用的字段最小视图（由数据字典适配）。"""

    name: str
    semantic_type: SemanticType
    ignored: bool = False
