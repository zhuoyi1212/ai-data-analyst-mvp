"""为 5 份示例数据生成离线 E2E 回放固件（profile/questions/plan/insights/followup）。

用法（在 backend/ 目录）：
    PYTHONPATH=. uv run python scripts/build_e2e_fixtures.py

关键约束：insights 固件中的数字不手写，而是先在真实管线上执行规范方案、
构建 ResultDigest 后取其展示精度值，从生成侧保证「数字接地」。
固件全部写入 settings.fixture_dir（tests/fixtures/llm）。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd

from app.config import settings
from app.schemas.common import AggFunc, ChartType, CorrMethod, OutlierMethod, TimeGranularity
from app.schemas.dictionary import DataDictionary, FieldProfile
from app.schemas.insight import GroundedNumber, Insight, InsightSet
from app.schemas.plan import (
    AggregateParams,
    AggregateStep,
    AnalysisPlan,
    CorrelationParams,
    CorrelationStep,
    GroupByParams,
    GroupByStep,
    OutlierFlagParams,
    OutlierFlagStep,
    ShareParams,
    ShareStep,
    TimeSeriesParams,
    TimeSeriesStep,
    TopNParams,
    TopNStep,
)
from app.schemas.common import Confidence, SemanticType
from app.services.executor import execute
from app.services.insight_context import build_result_digest
from app.services.insight_validator import validate_insight_set
from app.services.llm.fixtures import question_variant
from app.services.parser import friendly_dtype
from app.services.planner import _business_errors, confirm_plan
from app.services.quality_actions import apply_decisions
from app.services.quality_checker import run_quality_checks
from app.services.storage import SessionStore
from app.services.validator import acknowledge, run_validation
from scripts.generate_sample_data import DATASETS_CONFIG, GOLDEN_PATH

FIXTURE_DIR = settings.fixture_dir

# 每数据集：字段中文含义/单位、一个 medium 待确认字段（展示候选确认机制）、
# 规范方案（全量单步聚合）、7 条推荐问题、3 条追问。
DATASET_META: dict[str, dict] = {
    "sales_orders": {
        "meanings": {
            "订单号": ("订单唯一编号", None), "日期": ("下单日期", None),
            "区域": ("订单所属销售区域", None), "品类": ("商品品类", None),
            "商品": ("具体商品名称", None), "数量": ("销售数量", "件"),
            "单价": ("商品单价", "元"), "金额": ("订单金额", "元"),
            "渠道": ("销售渠道", None),
        },
        "medium": {"渠道": ["销售渠道（线上商城/门店等）", "下单渠道", "分销渠道"]},
        "canonical": {
            "question": "全部有效订单的销售总额是多少？",
            "metric": "金额", "func": "sum", "func_zh": "求和",
            "label": "销售总额", "chart_hint": "metric",
        },
        "questions": [
            ("全部有效订单的销售总额是多少？", "overview",
             "「金额」是核心销售指标，适合先看总体规模", ["金额"], "aggregate"),
            ("各月销售额如何变化？", "trend",
             "「日期」覆盖全年，可对「金额」按月观察时间趋势", ["日期", "金额"], "time_series"),
            ("不同品类的销售额如何对比？", "comparison",
             "「品类」是分类维度，可对「金额」分组对比", ["品类", "金额"], "group_by"),
            ("各销售渠道的金额占比如何？", "share",
             "「渠道」是枚举维度，可拆解「金额」的占比", ["渠道", "金额"], "share"),
            ("销售额最高的区域是哪些？", "ranking",
             "「区域」为地理维度，可对「金额」做排名", ["区域", "金额"], "top_n"),
            ("金额字段是否存在异常大单？", "anomaly",
             "「金额」可能存在录入错误导致的极端值，需要离群检测", ["金额"], "outlier_flag"),
            ("数量与金额之间的相关性如何？", "correlation",
             "「数量」与「金额」理论联动，可量化其相关关系", ["数量", "金额"], "correlation"),
        ],
        "followups": [
            ("各区域销售额排名前五是哪些？", "「区域」为地理维度，可对「金额」继续下钻排名",
             ["区域", "金额"], "top_n", {"dimension": "区域", "metric": "金额", "func": "sum", "n": 5}),
            ("销售额按月趋势如何？", "「日期」为日期字段，可对「金额」按月汇总看趋势",
             ["日期", "金额"], "time_series", {"granularity": "month"}),
            ("各品类销售额占比如何？", "「品类」是分类维度，可对「金额」做占比拆解",
             ["品类", "金额"], "share", {"dimension": "品类", "metric": "金额", "func": "sum"}),
        ],
    },
    "operations_daily": {
        "meanings": {
            "date": ("统计日期", None), "dau": ("日活跃用户数", "人"),
            "new_users": ("当日新增注册用户数", "人"),
            "sessions": ("当日会话次数", "次"),
            "avg_duration_sec": ("平均会话时长", "秒"),
            "tickets": ("当日客服工单数", "单"),
            "satisfaction": ("用户满意度评分", "分"),
        },
        "medium": {"tickets": ["当日新增工单数量", "客服处理完成工单数", "异常事件计数"]},
        "canonical": {
            "question": "运营周期内的日均活跃用户数是多少？",
            "metric": "dau", "func": "mean", "func_zh": "平均值",
            "label": "日均活跃用户数", "chart_hint": "metric",
        },
        "questions": [
            ("运营周期内的日均活跃用户数是多少？", "overview",
             "「dau」是运营核心指标，适合先看总体均值", ["dau"], "aggregate"),
            ("日活跃用户数随时间如何变化？", "trend",
             "「date」为日期字段，可对「dau」观察时间趋势", ["date", "dau"], "time_series"),
            ("会话数是否存在异常峰值？", "anomaly",
             "「sessions」存在明显高于常规水平的峰值，需要离群检测", ["sessions"], "outlier_flag"),
            ("日活跃用户数与会话数的相关性如何？", "correlation",
             "「dau」与「sessions」通常同向变动，可量化相关关系", ["dau", "sessions"], "correlation"),
            ("周期内工单总量是多少？", "overview",
             "「tickets」为每日工单数，可汇总得到总量", ["tickets"], "aggregate"),
            ("新增用户数随时间如何变化？", "trend",
             "「date」为日期字段，可对「new_users」观察趋势", ["date", "new_users"], "time_series"),
            ("平均会话时长与满意度的相关性如何？", "correlation",
             "「avg_duration_sec」与「satisfaction」可能存在相关关系",
             ["avg_duration_sec", "satisfaction"], "correlation"),
        ],
        "followups": [
            ("会话数随时间的趋势如何？", "「date」为日期字段，可对「sessions」按时间下钻",
             ["date", "sessions"], "time_series", {"granularity": "week"}),
            ("哪些天的会话数属于离群峰值？", "「sessions」已有异常峰值，可继续做离群标记",
             ["sessions"], "outlier_flag", {"column": "sessions", "method": "iqr", "threshold": 1.5}),
            ("新增用户与日活的相关性如何？", "「new_users」与「dau」通常相关，可量化验证",
             ["new_users", "dau"], "correlation", {}),
        ],
    },
    "marketing_campaigns": {
        "meanings": {
            "date": ("投放日期", None), "channel": ("投放渠道", None),
            "campaign": ("营销活动名称", None), "spend": ("广告花费", "元"),
            "impressions": ("曝光量", "次"), "clicks": ("点击量", "次"),
            "conversions": ("转化量", "次"), "revenue": ("带来收入", "元"),
        },
        "medium": {"campaign": ["营销活动名称", "活动主题", "投放计划名称"]},
        "canonical": {
            "question": "全部有效投放的总花费是多少？",
            "metric": "spend", "func": "sum", "func_zh": "求和",
            "label": "投放总花费", "chart_hint": "metric",
        },
        "questions": [
            ("全部有效投放的总花费是多少？", "overview",
             "「spend」是营销核心投入指标，适合先看总规模", ["spend"], "aggregate"),
            ("每日广告花费如何变化？", "trend",
             "「date」覆盖整个投放周期，可对「spend」看时间趋势", ["date", "spend"], "time_series"),
            ("各渠道的广告花费如何对比？", "comparison",
             "「channel」是枚举维度，可对「spend」分组对比", ["channel", "spend"], "group_by"),
            ("各渠道转化量的占比如何？", "share",
             "「channel」是枚举维度，可拆解「conversions」的占比",
             ["channel", "conversions"], "share"),
            ("收入最高的营销活动是哪些？", "ranking",
             "「campaign」是活动维度，可按「revenue」排名", ["campaign", "revenue"], "top_n"),
            ("收入字段是否存在异常高值？", "anomaly",
             "「revenue」可能受异常投放影响出现极端值", ["revenue"], "outlier_flag"),
            ("点击量与转化量的相关性如何？", "correlation",
             "「clicks」与「conversions」构成漏斗联动，可验证相关关系",
             ["clicks", "conversions"], "correlation"),
        ],
        "followups": [
            ("各渠道花费对比如何？", "「channel」是枚举维度，可对「spend」分组下钻",
             ["channel", "spend"], "group_by", {"dimension": "channel", "metric": "spend", "func": "sum"}),
            ("收入随时间的趋势如何？", "「date」为日期字段，可对「revenue」按月汇总",
             ["date", "revenue"], "time_series", {"granularity": "month"}),
            ("花费与转化量的相关性如何？", "「spend」与「conversions」的投入产出关系值得量化",
             ["spend", "conversions"], "correlation", {}),
        ],
    },
    "inventory_movements": {
        "meanings": {
            "date": ("业务日期", None), "sku": ("商品 SKU 编号", None),
            "category": ("商品品类", None), "warehouse": ("所在仓库", None),
            "stock_in": ("入库数量", "件"), "stock_out": ("出库数量", "件"),
            "ending_stock": ("期末库存数量", "件"), "unit_cost": ("单位成本", "元"),
        },
        "medium": {"unit_cost": ["单位入库成本", "采购单价", "加权平均成本价"]},
        "canonical": {
            "question": "全部库存记录的平均期末库存是多少？",
            "metric": "ending_stock", "func": "mean", "func_zh": "平均值",
            "label": "平均期末库存", "chart_hint": "metric",
        },
        "questions": [
            ("全部库存记录的平均期末库存是多少？", "overview",
             "「ending_stock」是库存核心指标，适合先看平均水平", ["ending_stock"], "aggregate"),
            ("期末库存随时间如何变化？", "trend",
             "「date」覆盖两个月，可对「ending_stock」观察趋势",
             ["date", "ending_stock"], "time_series"),
            ("不同品类的期末库存如何对比？", "comparison",
             "「category」是分类维度，可对「ending_stock」分组对比",
             ["category", "ending_stock"], "group_by"),
            ("各仓库出库量的占比如何？", "share",
             "「warehouse」是枚举维度，可拆解「stock_out」的占比",
             ["warehouse", "stock_out"], "share"),
            ("出库量最高的品类是哪些？", "ranking",
             "「category」是分类维度，可按「stock_out」排名",
             ["category", "stock_out"], "top_n"),
            ("期末库存是否存在异常负值？", "anomaly",
             "「ending_stock」正常应为非负数，需要离群检测识别异常",
             ["ending_stock"], "outlier_flag"),
            ("入库量与出库量的相关性如何？", "correlation",
             "「stock_in」与「stock_out」反映进销联动，可量化相关关系",
             ["stock_in", "stock_out"], "correlation"),
        ],
        "followups": [
            ("各品类平均期末库存如何对比？", "「category」是分类维度，可对「ending_stock」分组",
             ["category", "ending_stock"], "group_by",
             {"dimension": "category", "metric": "ending_stock", "func": "mean"}),
            ("出库量随时间的趋势如何？", "「date」为日期字段，可对「stock_out」按周汇总",
             ["date", "stock_out"], "time_series", {"granularity": "week"}),
            ("入库量最高的仓库是哪些？", "「warehouse」是枚举维度，可按「stock_in」排名",
             ["warehouse", "stock_in"], "top_n",
             {"dimension": "warehouse", "metric": "stock_in", "func": "sum", "n": 3}),
        ],
    },
    "customer_tickets": {
        "meanings": {
            "ticket_id": ("工单唯一编号", None), "created_at": ("工单创建时间", None),
            "channel": ("工单来源渠道", None), "issue_type": ("问题类型", None),
            "priority": ("工单优先级", None), "handle_hours": ("处理时长", "小时"),
            "is_resolved": ("是否已解决", None), "satisfaction": ("满意度评分", "分"),
        },
        "medium": {"is_resolved": ["是否已解决标记", "解决状态布尔字段", "回访成功标识"]},
        "canonical": {
            "question": "全部工单的平均处理时长是多少？",
            "metric": "handle_hours", "func": "mean", "func_zh": "平均值",
            "label": "平均处理时长", "chart_hint": "metric",
        },
        "questions": [
            ("全部工单的平均处理时长是多少？", "overview",
             "「handle_hours」是服务效率核心指标，适合先看平均水平", ["handle_hours"], "aggregate"),
            ("工单处理时长随时间如何变化？", "trend",
             "「created_at」为时间字段，可对「handle_hours」观察趋势",
             ["created_at", "handle_hours"], "time_series"),
            ("不同渠道的处理时长如何对比？", "comparison",
             "「channel」是枚举维度，可对「handle_hours」分组对比",
             ["channel", "handle_hours"], "group_by"),
            ("各渠道工单量的占比如何？", "share",
             "「channel」是枚举维度，可按工单来源拆解占比（结合「ticket_id」）",
             ["channel", "ticket_id"], "share"),
            ("处理时长最长的问题类型是哪些？", "ranking",
             "「issue_type」是分类维度，可按「handle_hours」排名",
             ["issue_type", "handle_hours"], "top_n"),
            ("处理时长是否存在异常超长工单？", "anomaly",
             "「handle_hours」可能存在异常滞留工单，需要离群检测",
             ["handle_hours"], "outlier_flag"),
            ("处理时长与满意度的相关性如何？", "correlation",
             "「handle_hours」与「satisfaction」可能负向相关，可量化验证",
             ["handle_hours", "satisfaction"], "correlation"),
        ],
        "followups": [
            ("不同优先级的处理时长如何对比？", "「priority」是分类维度，可对「handle_hours」分组",
             ["priority", "handle_hours"], "group_by",
             {"dimension": "priority", "metric": "handle_hours", "func": "mean"}),
            ("各问题类型工单量占比如何？", "「issue_type」是枚举维度，可拆解工单结构",
             ["issue_type", "ticket_id"], "share",
             {"dimension": "issue_type", "metric": "ticket_id", "func": "count"}),
            ("满意度与处理时长的相关性如何？", "「satisfaction」与「handle_hours」可能相关",
             ["satisfaction", "handle_hours"], "correlation", {}),
        ],
    },
}


def _write_fixture(stage: str, name: str, payload: dict) -> None:
    d = FIXTURE_DIR / stage
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(
        json.dumps({"response": payload}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def _high_dictionary(df, semantics: dict[str, SemanticType]) -> DataDictionary:
    fields = [
        FieldProfile(name=col, physical_type=friendly_dtype(df[col]),
                     semantic_type=semantics[col], meaning="",
                     confidence=Confidence.high, confirmed=True, confirmed_by_user=True)
        for col in df.columns
    ]
    return DataDictionary(session_id="x", fields=fields, complete=True)


def build_one(name: str, store: SessionStore, golden: dict) -> None:
    cfg = DATASETS_CONFIG[name]
    meta_cfg = DATASET_META[name]
    df = cfg["builder"]()

    # 真实管线：上传 → 高质量字典（生成固件用，独立于 profile 固件）→ 质量处理
    sid = store.create_from_bytes(f"{name}.csv",
                                  df.to_csv(index=False).encode("utf-8"))["session_id"]
    dictionary = _high_dictionary(df, cfg["semantics"])
    dictionary.session_id = sid
    store.write_artifact(sid, "dictionary", dictionary.model_dump(mode="json"))
    run_quality_checks(sid, store)
    apply_decisions(sid, golden["decisions"], store)

    # profile 固件：含一个 medium 待确认字段，触发用户确认界面
    semantics = cfg["semantics"]
    profile_fields = []
    for col in df.columns:
        meaning, unit = meta_cfg["meanings"][col]
        is_medium = col in meta_cfg["medium"]
        profile_fields.append({
            "name": col,
            "semantic_type": semantics[col].value,
            "meaning": meaning,
            "unit": unit,
            "candidates": meta_cfg["medium"][col] if is_medium else [],
            "confidence": "medium" if is_medium else "high",
        })
    _write_fixture("profile", name, {"fields": profile_fields})

    # questions 固件
    questions = [
        {"text": t, "category": cat, "rationale": rat, "fields": flds,
         "target_op": op, "confidence": "high"}
        for (t, cat, rat, flds, op) in meta_cfg["questions"]
    ]
    _write_fixture("questions", f"{name}_questions", {"questions": questions})

    # plan 固件 + 在真实管线上锁定/执行/校验，以取得接地数字
    canon = meta_cfg["canonical"]
    plan = AnalysisPlan(
        question=canon["question"],
        data_scope="数据质量处理后的全部数据，无额外筛选条件",
        steps=[AggregateStep(
            step_id="agg",
            op="aggregate",
            params=AggregateParams(column=canon["metric"], func=AggFunc(canon["func"])),
            description=f"对字段「{canon['metric']}」执行{canon['func_zh']}",
            depends_on=[],
        )],
        expected_shape="单行单列指标卡",
        chart_hint=canon["chart_hint"],
    )
    _write_fixture("plan", f"{name}_plan", plan.model_dump(mode="json"))

    confirm_plan(sid, store, plan.model_dump(mode="json"))
    ledger = execute(sid, store)
    report = run_validation(sid, store)
    assert report.overall == "pass", f"{name} 规范方案校验未通过：{report.model_dump()}"

    # insights 固件：数字取自 ResultDigest（展示精度），不手写
    result_df = pd.read_parquet(store.session_dir(sid) / "result.parquet")
    plan_artifact = confirm_plan(sid, store)  # 已锁定，幂等读取
    digest = build_result_digest(ledger, plan_artifact, result_df, report)
    v = digest.points[0].value
    metric, func_zh, label = canon["metric"], canon["func_zh"], canon["label"]
    insights = {
        "insights": [
            {
                "text": f"「{label}」的{func_zh}为 {v}，基于质量处理后的全部数据计算。",
                "type": "key_finding",
                "evidence_refs": ["agg"],
                "numbers": [{
                    "value": v, "label": f"{label}{func_zh}",
                    "ref_step": "agg", "ref_kind": "metric", "ref_keys": {},
                }],
                "confidence": "high",
                "confidence_reason": "全量描述性聚合，五项校验全部通过，无风险因素",
                "needs_further_validation": False,
                "disclaimer": None,
            },
            {
                "text": f"本次聚合结果 {v} 由计算引擎对字段「{metric}」执行{func_zh}得出，证据链完整可追溯。",
                "type": "key_finding",
                "evidence_refs": ["agg"],
                "numbers": [{
                    "value": v, "label": f"{label}{func_zh}",
                    "ref_step": "agg", "ref_kind": "metric", "ref_keys": {},
                }],
                "confidence": "high",
                "confidence_reason": "全量描述性聚合，五项校验全部通过，无风险因素",
                "needs_further_validation": False,
                "disclaimer": None,
            },
        ]
    }
    _write_fixture("insights", f"{name}_insights", insights)

    # followup 固件
    followups = {"questions": [
        {"text": t, "rationale": rat, "fields": flds, "target_op": op, "plan_hint": hint}
        for (t, rat, flds, op, hint) in meta_cfg["followups"]
    ]}
    _write_fixture("followup", f"{name}_followup", followups)
    print(f"✓ {name}: 5 阶段固件已生成（接地值 {label}={v}，校验 {report.overall}）")

    # 追问多轮：为每条追问生成 plan + insights 变体固件
    build_followup_rounds(sid, store, name, meta_cfg, dictionary)


def _followup_chart(op: str) -> ChartType:
    return {
        "group_by": ChartType.bar,
        "share": ChartType.pie,
        "top_n": ChartType.bar,
        "time_series": ChartType.line,
        "correlation": ChartType.scatter,
        "outlier_flag": ChartType.line_outlier,
    }[op]


def _build_followup_plan(
    question: str, op: str, hint: dict, flds: list[str]
) -> AnalysisPlan:
    """根据追问的 target_op 与 plan_hint 构造有限算子方案。"""
    desc_op = {
        "group_by": "分组聚合", "share": "占比拆解", "top_n": "排名截取",
        "time_series": "时间序列聚合", "correlation": "相关系数计算",
        "outlier_flag": "离群值标记",
    }[op]
    step_id = op
    if op == "group_by":
        func = AggFunc(hint.get("func", "sum"))
        metric = None if func == AggFunc.count else hint["metric"]
        params = GroupByParams(dimension=hint["dimension"], metric=metric, func=func)
        step = GroupByStep(step_id=step_id, op="group_by", params=params,
                           description=f"按「{params.dimension}」对「{params.metric or '记录数'}」{params.func.value}")
    elif op == "share":
        func = AggFunc(hint.get("func", "sum"))
        metric = None if func == AggFunc.count else hint["metric"]
        params = ShareParams(dimension=hint["dimension"], metric=metric, func=func)
        step = ShareStep(step_id=step_id, op="share", params=params,
                         description=f"按「{params.dimension}」拆解「{params.metric or '记录数'}」的占比")
    elif op == "top_n":
        func = AggFunc(hint.get("func", "sum"))
        metric = None if func == AggFunc.count else hint["metric"]
        params = TopNParams(
            dimension=hint["dimension"], metric=metric, func=func,
            n=int(hint.get("n", 5)),
        )
        step = TopNStep(step_id=step_id, op="top_n", params=params,
                        description=f"按「{params.metric or '记录数'}」{params.func.value}，取「{params.dimension}」前 {params.n} 名")
    elif op == "time_series":
        date_col = flds[0]
        metric = flds[1] if len(flds) > 1 else None
        params = TimeSeriesParams(
            date_column=date_col, granularity=TimeGranularity(hint.get("granularity", "month")),
            metric=metric, func=AggFunc(hint.get("func", "sum")),
        )
        step = TimeSeriesStep(step_id=step_id, op="time_series", params=params,
                              description=f"按 {params.granularity.value} 对「{params.metric}」{params.func.value}")
    elif op == "correlation":
        params = CorrelationParams(column_x=flds[0], column_y=flds[1], method=CorrMethod.pearson)
        step = CorrelationStep(step_id=step_id, op="correlation", params=params,
                               description=f"计算「{params.column_x}」与「{params.column_y}」的皮尔逊相关系数")
    elif op == "outlier_flag":
        params = OutlierFlagParams(
            column=hint["column"], method=OutlierMethod(hint.get("method", "iqr")),
            threshold=float(hint.get("threshold", 1.5)),
        )
        step = OutlierFlagStep(step_id=step_id, op="outlier_flag", params=params,
                               description=f"用 {params.method.value} 方法标记「{params.column}」的离群值")
    else:
        raise ValueError(f"不支持的追问算子: {op}")
    return AnalysisPlan(
        question=question,
        data_scope="数据质量处理后的全部数据，无额外筛选条件",
        steps=[step],
        expected_shape=f"{desc_op}结果",
        chart_hint=_followup_chart(op),
    )


def _author_followup_insights(digest, plan: AnalysisPlan) -> InsightSet:
    """按 terminal_op 构造 2 条完全接地的洞察（数字来自 digest.points）。"""
    last = plan.steps[-1]
    sid = last.step_id
    points = digest.points
    insights: list[Insight] = []

    def gn(p, label: str) -> GroundedNumber:
        return GroundedNumber(
            value=p.value, label=label, ref_step=p.step_id,
            ref_kind=p.kind, ref_keys=dict(p.keys),
        )

    if digest.terminal_op in {"group_by", "top_n"}:
        dim = last.params.dimension
        rows = digest.rows  # [{dim, value}]
        top, bot = rows[0], rows[-1]
        p_top = next(p for p in points if p.keys == {dim: str(top[dim])})
        p_bot = next(p for p in points if p.keys == {dim: str(bot[dim])})
        insights = [
            Insight(
                text=f"在「{dim}」维度下，{top[dim]} 的数值最高，为 {top['value']}。",
                type="comparison", evidence_refs=[sid],
                numbers=[gn(p_top, f"{top[dim]}")],
                confidence=Confidence.high, confidence_reason="分组聚合结果，数字全部接地。",
            ),
            Insight(
                text=f"排序最末的成员是 {bot[dim]}，对应数值为 {bot['value']}。",
                type="comparison", evidence_refs=[sid],
                numbers=[gn(p_bot, f"{bot[dim]}")],
                confidence=Confidence.high, confidence_reason="分组聚合结果，数字全部接地。",
            ),
        ]
    elif digest.terminal_op == "share":
        dim = last.params.dimension
        rows = digest.rows  # [{dim, value, share_pct}]
        top = rows[0]
        p_val = next(p for p in points if p.keys == {dim: str(top[dim])} and not p.is_percent)
        p_pct = next(p for p in points
                     if p.keys == {dim: str(top[dim]), "metric": "share_pct"})
        insights = [
            Insight(
                text=f"占比最高的成员是 {top[dim]}，占比为 {top['share_pct']}%，对应数值为 {top['value']}。",
                type="comparison", evidence_refs=[sid],
                numbers=[gn(p_val, f"{top[dim]} 数值"), gn(p_pct, f"{top[dim]} 占比")],
                confidence=Confidence.high, confidence_reason="占比拆解结果，数字全部接地。",
            ),
        ]
        if len(rows) > 1:
            second = rows[1]
            p2_pct = next(p for p in points
                          if p.keys == {dim: str(second[dim]), "metric": "share_pct"})
            insights.append(Insight(
                text=f"其次为 {second[dim]}，占比为 {second['share_pct']}%。",
                type="comparison", evidence_refs=[sid],
                numbers=[gn(p2_pct, f"{second[dim]} 占比")],
                confidence=Confidence.high, confidence_reason="占比拆解结果，数字全部接地。",
            ))
    elif digest.terminal_op == "time_series":
        date_col = last.params.date_column
        rows = [r for r in digest.rows if r["value"] is not None]
        top = max(rows, key=lambda r: r["value"])
        bot = min(rows, key=lambda r: r["value"])
        p_top = next(p for p in points if p.keys == {date_col: str(top[date_col])})
        p_bot = next(p for p in points if p.keys == {date_col: str(bot[date_col])})
        insights = [
            Insight(
                text=f"{top[date_col]} 出现周期峰值，数值为 {top['value']}。",
                type="trend", evidence_refs=[sid],
                numbers=[gn(p_top, f"{top[date_col]}")],
                confidence=Confidence.high, confidence_reason="时间序列聚合结果，数字全部接地。",
            ),
            Insight(
                text=f"{bot[date_col]} 为周期谷值，数值为 {bot['value']}。",
                type="trend", evidence_refs=[sid],
                numbers=[gn(p_bot, f"{bot[date_col]}")],
                confidence=Confidence.high, confidence_reason="时间序列聚合结果，数字全部接地。",
            ),
        ]
    elif digest.terminal_op == "correlation":
        p_r = next(p for p in points if p.keys == {"field": "coefficient"})
        p_n = next(p for p in points if p.keys == {"field": "n"})
        r = p_r.value
        direction = "正相关" if r > 0 else ("负相关" if r < 0 else "无线性关系")
        strength = "强相关" if abs(r) >= 0.6 else ("中等相关" if abs(r) >= 0.3 else "弱相关")
        insights = [
            Insight(
                text=f"两字段的相关系数为 {r}（样本对数 {p_n.value:.0f}），呈{strength}{direction}（相关关系，不代表因果）。",
                type="risk", evidence_refs=[sid],
                numbers=[gn(p_r, "相关系数"), gn(p_n, "样本对数")],
                confidence=Confidence.medium, needs_further_validation=True,
                confidence_reason="相关分析仅反映相关关系。",
                disclaimer="相关关系，不代表因果",
            ),
            Insight(
                text=f"基于 {p_n.value:.0f} 对样本，相关系数为 {r}，呈{direction}（相关关系，不代表因果）。",
                type="risk", evidence_refs=[sid],
                numbers=[gn(p_r, "相关系数"), gn(p_n, "样本对数")],
                confidence=Confidence.medium, needs_further_validation=True,
                confidence_reason="相关分析仅反映相关关系。",
                disclaimer="相关关系，不代表因果",
            ),
        ]
    elif digest.terminal_op == "outlier_flag":
        p_count = next(p for p in points if p.keys == {"field": "outlier_count"})
        p_rate = next(p for p in points if p.keys == {"field": "outlier_rate_pct"})
        p_lo = next(p for p in points if p.keys == {"field": "lower_bound"})
        p_hi = next(p for p in points if p.keys == {"field": "upper_bound"})
        insights = [
            Insight(
                text=f"在 IQR 围栏区间 [{p_lo.value}, {p_hi.value}] 之外，共识别 {p_count.value:.0f} 个离群点。",
                type="anomaly", evidence_refs=[sid],
                numbers=[gn(p_lo, "下界"), gn(p_hi, "上界"), gn(p_count, "离群点数")],
                confidence=Confidence.high, confidence_reason="离群检测结果，数字全部接地。",
            ),
            Insight(
                text=f"离群点占比为 {p_rate.value}%，需结合业务判断是否为真实异常。",
                type="anomaly", evidence_refs=[sid],
                numbers=[gn(p_rate, "离群占比")],
                confidence=Confidence.high, confidence_reason="离群检测结果，数字全部接地。",
            ),
        ]
    else:
        raise ValueError(f"未支持的 terminal_op: {digest.terminal_op}")
    return InsightSet(insights=insights)


def build_followup_rounds(
    sid: str, store: SessionStore, name: str, meta_cfg: dict, dictionary: DataDictionary
) -> None:
    """为每条追问生成 plan + insights 变体固件（数字取自真实引擎结果）。"""
    base = f"{name}"
    for text, _rat, flds, op, hint in meta_cfg["followups"]:
        plan = _build_followup_plan(text, op, hint, flds)
        errs = _business_errors(plan, dictionary)
        assert not errs, f"{name} 追问「{text}」方案业务校验失败: {errs}"

        plan_variant = question_variant(f"{base}_plan", "plan", text)
        _write_fixture("plan", plan_variant, plan.model_dump(mode="json"))

        confirm_plan(sid, store, plan.model_dump(mode="json"))
        ledger = execute(sid, store)
        report = run_validation(sid, store)
        assert report.overall != "fail", (
            f"{name} 追问「{text}」校验失败: {report.model_dump()}"
        )
        if report.overall == "warn":
            acknowledge(sid, store)

        result_df = pd.read_parquet(store.session_dir(sid) / "result.parquet")
        plan_artifact = confirm_plan(sid, store)
        digest = build_result_digest(ledger, plan_artifact, result_df, report)
        insight_set = _author_followup_insights(digest, plan)
        v_errs = validate_insight_set(insight_set, digest)
        assert not v_errs, f"{name} 追问「{text}」洞察接地校验失败: {v_errs}"

        ins_variant = question_variant(f"{base}_insights", "insights", text)
        _write_fixture(
            "insights", ins_variant,
            {"insights": [i.model_dump(mode="json") for i in insight_set.insights]},
        )
    print(f"  ✓ 追问变体固件: {len(meta_cfg['followups'])} 条 plan + insights")


def main() -> None:
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["datasets"]
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "store")
        for name in DATASETS_CONFIG:
            build_one(name, store, golden[name])
    print(f"✓ 固件目录：{FIXTURE_DIR}")


if __name__ == "__main__":
    main()
