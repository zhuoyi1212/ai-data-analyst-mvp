"""生成附录 B 的 5 份示例 CSV 与黄金值固件（固定种子，字节稳定）。

用法（在 backend/ 目录）：
    uv run python scripts/generate_sample_data.py

每份数据预埋规范要求的质量问题；脚本直接复用真实的质量检测/处理管线与
计算引擎产出 snapshot_rows 与聚合黄金值，避免两套口径漂移。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from app.config import settings
from app.schemas.common import AggFunc, Confidence, SemanticType
from app.schemas.dictionary import DataDictionary, FieldProfile
from app.schemas.plan import (
    AggregateParams,
    AggregateStep,
    GroupByParams,
    GroupByStep,
    TimeSeriesParams,
    TimeSeriesStep,
)
from app.services.engine.ops import run_step
from app.services.parser import friendly_dtype
from app.services.quality_actions import apply_decisions
from app.services.quality_checker import run_quality_checks
from app.services.storage import SessionStore

SAMPLE_DIR = settings.sample_data_dir
GOLDEN_PATH = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden" / "golden.json"

REGIONS = ["华东", "华南", "华北", "西南", "华中"]
CATEGORIES = ["数码", "家居", "服饰", "食品"]
PRODUCTS = [f"商品{c}-{i:02d}" for c in CATEGORIES for i in range(5)]
CHANNELS_CN = ["线上商城", "门店", "经销商"]


# ---------------------------------------------------------------- 数据构造

def build_sales_orders() -> pd.DataFrame:
    rng = np.random.default_rng(20240101)
    n = 3000
    days = pd.date_range("2024-01-01", "2024-12-28", freq="D")
    rows = []
    for i in range(n):
        day = days[rng.integers(0, len(days))]
        date_str = day.strftime("%Y/%m/%d") if i % 20 == 0 else day.strftime("%Y-%m-%d")
        qty = int(rng.integers(1, 5))
        price = int(rng.choice([49, 79, 99]))
        region = REGIONS[rng.integers(0, 5)]
        if i % 75 == 0:
            region = None  # 区域缺失
        amount = float(qty * price)
        rows.append(
            (f"SO{100000 + i}", date_str, region,
             CATEGORIES[rng.integers(0, 4)], PRODUCTS[rng.integers(0, 20)],
             qty, price, amount, CHANNELS_CN[rng.integers(0, 3)])
        )
    df = pd.DataFrame(rows, columns=[
        "订单号", "日期", "区域", "品类", "商品", "数量", "单价", "金额", "渠道"])
    # 金额离群（8 笔异常大单）
    for i in rng.choice(n, size=8, replace=False):
        df.loc[i, "金额"] = 80000.0
    # 完全重复行
    dup = df.iloc[[2, 17, 88, 203]].copy()
    return pd.concat([df, dup], ignore_index=True)


def build_operations_daily() -> pd.DataFrame:
    rng = np.random.default_rng(20240202)
    # 约 6 个月日粒度，预埋 6 个日期缺口
    all_days = pd.date_range("2024-01-03", "2024-06-30", freq="D")
    gaps = {pd.Timestamp("2024-02-11"), pd.Timestamp("2024-03-02"),
            pd.Timestamp("2024-03-18"), pd.Timestamp("2024-04-09"),
            pd.Timestamp("2024-05-21"), pd.Timestamp("2024-06-06")}
    days = [d for d in all_days if d not in gaps]
    n = len(days)
    dau = rng.integers(4200, 5800, size=n).astype(float)
    new_users = rng.integers(120, 480, size=n).astype(float)
    sessions = rng.integers(9000, 14000, size=n).astype(float)
    duration = np.round(rng.uniform(210, 420, size=n), 1)
    tickets = rng.integers(20, 120, size=n)
    satisfaction = np.round(rng.uniform(3.6, 5.0, size=n), 2)
    # -1 哨兵缺失（dau）
    for i in [5, 33, 71, 102, 140, 166]:
        dau[i] = -1
    # 指标空值（new_users）
    for i in [8, 44, 90, 121, 150, 172, 10, 60]:
        new_users[i] = np.nan
    # 离群峰值（sessions）
    for i in [19, 77, 113, 155]:
        sessions[i] = 39000.0
    return pd.DataFrame({
        "date": [d.strftime("%Y-%m-%d") for d in days],
        "dau": dau.astype("int64"),
        "new_users": new_users,
        "sessions": sessions.astype("int64"),
        "avg_duration_sec": duration,
        "tickets": tickets,
        "satisfaction": satisfaction,
    })


def build_marketing_campaigns() -> pd.DataFrame:
    rng = np.random.default_rng(20240303)
    days = pd.date_range("2024-03-01", "2024-05-29", freq="D")  # 90 天
    channels = ["wechat", "web", "douyin", "weibo"]
    campaigns = ["品牌种草", "节日大促", "拉新投放", "复购唤醒", "直播专场", "搜索广告"]
    rows = []
    for d in days:
        for ch in channels:
            for camp in campaigns:
                spend = round(float(rng.uniform(300, 3000)), 2)
                spend_s = (
                    f"¥{spend:,.2f}" if rng.random() < 0.12 else f"{spend:.2f}"
                )
                impressions = int(rng.integers(5000, 80000))
                clicks = int(impressions * rng.uniform(0.01, 0.08))
                conversions = int(clicks * rng.uniform(0.02, 0.12))
                revenue = round(conversions * rng.uniform(60, 320), 2)
                rows.append((d.strftime("%Y-%m-%d"), ch, camp, spend_s,
                             impressions, clicks, conversions, revenue))
    df = pd.DataFrame(rows, columns=[
        "date", "channel", "campaign", "spend", "impressions",
        "clicks", "conversions", "revenue"])
    # 枚举大小写不一
    for i in rng.choice(len(df), size=220, replace=False):
        df.loc[i, "channel"] = rng.choice(["WeChat", "WEB", "Douyin", "WEIBO"])
    # 离群花费（普通数字字符串形式，确保可被 IQR 检测）
    for i in rng.choice(len(df), size=8, replace=False):
        df.loc[i, "spend"] = "65000.00"
    dup = df.iloc[[10, 500, 1300, 2000]].copy()
    return pd.concat([df, dup], ignore_index=True)


def build_inventory_movements() -> pd.DataFrame:
    rng = np.random.default_rng(20240404)
    days = pd.date_range("2024-05-01", "2024-06-29", freq="D")  # 60 天
    skus = [f"SKU-{1001 + i}" for i in range(36)]
    categories = ["数码配件", "家居百货", "食品饮料", "个护清洁", "服饰"]
    warehouses = ["华东一仓", "华南二仓", "华北三仓"]
    rows = []
    for d in days:
        for sku in skus:
            stock_in = int(rng.integers(0, 120))
            stock_out = int(rng.integers(0, 100))
            ending = int(rng.integers(20, 320))
            cost = round(float(rng.uniform(8, 160)), 2)
            cost_s = f"¥{cost:,.2f}" if rng.random() < 0.3 else f"{cost:.2f}"
            rows.append((d.strftime("%Y-%m-%d"), sku,
                         categories[int(sku[-3:]) % 5],
                         warehouses[rng.integers(0, 3)],
                         stock_in, stock_out, ending, cost_s))
    df = pd.DataFrame(rows, columns=[
        "date", "sku", "category", "warehouse", "stock_in",
        "stock_out", "ending_stock", "unit_cost"])
    # stock_in 缺失
    for i in rng.choice(len(df), size=25, replace=False):
        df.loc[i, "stock_in"] = np.nan
    # 负库存异常（IQR 低端离群）
    for i in rng.choice(len(df), size=12, replace=False):
        df.loc[i, "ending_stock"] = int(rng.integers(-500, -300))
    dup = df.iloc[[3, 900, 1800]].copy()
    return pd.concat([df, dup], ignore_index=True)


def build_customer_tickets() -> pd.DataFrame:
    rng = np.random.default_rng(20240505)
    n = 2600
    start = pd.Timestamp("2024-03-01")
    channels = ["web", "phone", "app"]
    issue_types = ["登录问题", "支付失败", "退款进度", "功能咨询", "物流查询"]
    priorities = ["P0", "P1", "P2", "P3"]
    rows = []
    for i in range(n):
        d = start + pd.Timedelta(days=int(rng.integers(0, 122)),
                                 hours=int(rng.integers(0, 24)),
                                 minutes=int(rng.integers(0, 60)))
        created = d.strftime("%Y/%m/%d %H:%M") if i % 12 == 0 else d.strftime("%Y-%m-%d %H:%M")
        ch = channels[rng.integers(0, 3)]
        if rng.random() < 0.12:
            ch = {"web": "Web", "phone": "Phone", "app": "APP"}[ch]
        hours = round(float(rng.uniform(0.5, 36)), 1)
        if i % 58 == 0:
            hours = np.nan  # 处理时长缺失
        rows.append((
            f"T{200000 + i}", created, ch, issue_types[rng.integers(0, 5)],
            priorities[rng.integers(0, 4)], hours,
            "是" if rng.random() < 0.86 else "否",
            int(rng.integers(1, 6)),
        ))
    df = pd.DataFrame(rows, columns=[
        "ticket_id", "created_at", "channel", "issue_type", "priority",
        "handle_hours", "is_resolved", "satisfaction"])
    # 超长处理时长离群
    for i in rng.choice(n, size=10, replace=False):
        df.loc[i, "handle_hours"] = round(float(rng.uniform(380, 520)), 1)
    return df


# ---------------------------------------------------------------- 语义/决策/黄金

DATASETS_CONFIG = {
    "sales_orders": {
        "builder": build_sales_orders,
        "semantics": {
            "订单号": SemanticType.id, "日期": SemanticType.date,
            "区域": SemanticType.geo, "品类": SemanticType.dimension,
            "商品": SemanticType.dimension, "数量": SemanticType.metric,
            "单价": SemanticType.metric, "金额": SemanticType.metric,
            "渠道": SemanticType.dimension,
        },
        "missing_policy": {"区域": "drop_rows"},
        "outlier_policy": {"金额": "exclude"},
        "required_issue_prefixes": ["format:date_text:日期", "missing:区域",
                                     "outlier:金额", "duplicate:rows"],
    },
    "operations_daily": {
        "builder": build_operations_daily,
        "semantics": {
            "date": SemanticType.date, "dau": SemanticType.metric,
            "new_users": SemanticType.metric, "sessions": SemanticType.metric,
            "avg_duration_sec": SemanticType.metric, "tickets": SemanticType.metric,
            "satisfaction": SemanticType.metric,
        },
        "missing_policy": {"dau": "fill_mean", "new_users": "fill_mean"},
        "outlier_policy": {"sessions": "mark"},
        "required_issue_prefixes": ["format:sentinel:dau", "missing:dau",
                                     "missing:new_users", "outlier:sessions"],
    },
    "marketing_campaigns": {
        "builder": build_marketing_campaigns,
        "semantics": {
            "date": SemanticType.date, "channel": SemanticType.dimension,
            "campaign": SemanticType.dimension, "spend": SemanticType.metric,
            "impressions": SemanticType.metric, "clicks": SemanticType.metric,
            "conversions": SemanticType.metric, "revenue": SemanticType.metric,
        },
        "missing_policy": {},
        "outlier_policy": {"spend": "exclude"},
        "required_issue_prefixes": ["format:currency:spend", "format:enum_case:channel",
                                     "duplicate:rows", "outlier:spend"],
    },
    "inventory_movements": {
        "builder": build_inventory_movements,
        "semantics": {
            "date": SemanticType.date, "sku": SemanticType.id,
            "category": SemanticType.dimension, "warehouse": SemanticType.dimension,
            "stock_in": SemanticType.metric, "stock_out": SemanticType.metric,
            "ending_stock": SemanticType.metric, "unit_cost": SemanticType.metric,
        },
        "missing_policy": {"stock_in": "fill_mean"},
        "outlier_policy": {"ending_stock": "exclude"},
        "required_issue_prefixes": ["format:currency:unit_cost", "missing:stock_in",
                                     "outlier:ending_stock", "duplicate:rows"],
    },
    "customer_tickets": {
        "builder": build_customer_tickets,
        "semantics": {
            "ticket_id": SemanticType.id, "created_at": SemanticType.date,
            "channel": SemanticType.dimension, "issue_type": SemanticType.dimension,
            "priority": SemanticType.dimension, "handle_hours": SemanticType.metric,
            "is_resolved": SemanticType.dimension, "satisfaction": SemanticType.metric,
        },
        "missing_policy": {"handle_hours": "fill_mean"},
        "outlier_policy": {"handle_hours": "mark"},
        "required_issue_prefixes": ["format:date_text:created_at",
                                     "format:enum_case:channel",
                                     "missing:handle_hours", "outlier:handle_hours"],
    },
}


def _dictionary_for(df: pd.DataFrame, semantics: dict[str, SemanticType]) -> DataDictionary:
    fields = []
    for col in df.columns:
        fields.append(FieldProfile(
            name=col, physical_type=friendly_dtype(df[col]),
            semantic_type=semantics[col], meaning="", confidence=Confidence.high,
            confirmed=True, confirmed_by_user=True))
    return DataDictionary(session_id="x", fields=fields, complete=True)


def _decisions_for(issues, cfg: dict) -> dict:
    decisions = {}
    for issue in issues:
        iid = issue.issue_id
        if iid.startswith("format:"):
            decisions[iid] = {"action": "convert"}
        elif iid == "duplicate:rows":
            decisions[iid] = {"action": "drop_duplicates"}
        elif iid.startswith("missing:"):
            col = iid.split(":", 1)[1]
            decisions[iid] = {"action": cfg["missing_policy"].get(col, "keep")}
        elif iid.startswith("outlier:"):
            col = iid.split(":", 1)[1]
            decisions[iid] = {"action": cfg["outlier_policy"].get(col, "keep")}
    return decisions


def _golden_values(name: str, snapshot: pd.DataFrame) -> dict:
    def agg(col, func):
        r = run_step(snapshot, AggregateStep(
            step_id="a", op="aggregate",
            params=AggregateParams(column=col, func=AggFunc(func))))
        return round(float(r.df["value"].iloc[0]), 4)

    def group_top(dim, col):
        r = run_step(snapshot, GroupByStep(
            step_id="g", op="group_by",
            params=GroupByParams(dimension=dim, metric=col, func=AggFunc.sum)))
        return {"member": str(r.df[dim].iloc[0]),
                "value": round(float(r.df["value"].iloc[0]), 4)}

    def month_count(col):
        date_col = "日期" if "日期" in snapshot.columns else "date"
        if col is None:
            return None
        r = run_step(snapshot, TimeSeriesStep(
            step_id="t", op="time_series",
            params=TimeSeriesParams(date_column=date_col, granularity="month",
                                    metric=col, func=AggFunc.sum)))
        return int(len(r.df))

    if name == "sales_orders":
        top = group_top("区域", "金额")
        return {"amount_sum": agg("金额", "sum"),
                "qty_sum": agg("数量", "sum"),
                "top_region": top["member"], "top_region_amount": top["value"],
                "month_count": month_count("金额")}
    if name == "operations_daily":
        return {"dau_mean": agg("dau", "mean"), "sessions_max": agg("sessions", "max"),
                "tickets_sum": agg("tickets", "sum")}
    if name == "marketing_campaigns":
        top = group_top("channel", "spend")
        return {"spend_sum": agg("spend", "sum"),
                "conversions_sum": agg("conversions", "sum"),
                "top_channel": top["member"], "top_channel_spend": top["value"]}
    if name == "inventory_movements":
        return {"ending_stock_mean": agg("ending_stock", "mean"),
                "stock_out_sum": agg("stock_out", "sum"),
                "stock_in_mean": agg("stock_in", "mean")}
    # customer_tickets
    return {"handle_hours_mean": agg("handle_hours", "mean"),
            "satisfaction_mean": agg("satisfaction", "mean"),
            "resolved_count_distinct_rows": int(len(snapshot))}


def build_golden_entry(name: str, cfg: dict, storage_tmp: Path) -> dict:
    df = cfg["builder"]()
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    store = SessionStore(storage_tmp)
    meta = store.create_from_bytes(f"{name}.csv", csv_bytes)
    sid = meta["session_id"]
    dictionary = _dictionary_for(df, cfg["semantics"])
    dictionary.session_id = sid
    store.write_artifact(sid, "dictionary", dictionary.model_dump(mode="json"))

    report = run_quality_checks(sid, store)
    issue_ids = [i.issue_id for i in report.issues]
    for prefix in cfg["required_issue_prefixes"]:
        if prefix not in issue_ids:
            raise RuntimeError(f"{name}: 未检测到预期质量问题 {prefix}；实际：{issue_ids}")

    decisions = _decisions_for(report.issues, cfg)
    report = apply_decisions(sid, decisions, store)
    snapshot = store.load_snapshot(sid)

    counts: dict[str, int] = {}
    for i in report.issues:
        counts[i.type.value] = counts.get(i.type.value, 0) + 1

    return {
        "filename": f"{name}.csv",
        "raw_rows": len(df),
        "issue_ids": issue_ids,
        "issue_counts": counts,
        "decisions": decisions,
        "snapshot_rows": report.snapshot_rows,
        "snapshot_hash": report.snapshot_hash,
        "golden_values": _golden_values(name, snapshot),
    }


def main() -> None:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    golden: dict[str, dict] = {"datasets": {}}
    with tempfile.TemporaryDirectory() as tmp:
        for name, cfg in DATASETS_CONFIG.items():
            df = cfg["builder"]()
            csv_path = SAMPLE_DIR / f"{name}.csv"
            csv_path.write_bytes(df.to_csv(index=False).encode("utf-8"))
            entry = build_golden_entry(name, cfg, Path(tmp) / "store")
            golden["datasets"][name] = entry
            print(f"✓ {name}: {entry['raw_rows']} 行 → 快照 {entry['snapshot_rows']} 行，"
                  f"问题 {entry['issue_counts']}")
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(
        json.dumps(golden, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(f"✓ 黄金值写入 {GOLDEN_PATH}")


if __name__ == "__main__":
    main()
