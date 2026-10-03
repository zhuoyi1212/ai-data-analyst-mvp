"""Task 3：Signal Detection 与 Ranking 测试。

覆盖验收：
- TR-3.1 预埋信号（亏损成员/量利背离/异常/显著相关/结构悖论）全部被提取，
        magnitude 与 scan view 真实数据逐值一致；
- TR-3.2 rank 确定性幂等，top K 数量与截断正确。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.schemas.auto import Signal, SignalSet
from app.schemas.common import Confidence, SemanticType
from app.schemas.dictionary import DataDictionary, FieldProfile
from app.schemas.quality import QualityReport
from app.services.broad_scan import plan_scan
from app.services.bundle_executor import execute_bundle
from app.services.signals import build_signal_set, extract_signals
from app.services.storage import SessionStore


# ------------------------------------------------------------ 会话构造


def _craft_session(tmp_path: Path, df: pd.DataFrame) -> tuple:
    store = SessionStore(tmp_path / "store")
    sid = store.create_from_bytes(
        "data.csv", df.to_csv(index=False).encode("utf-8")
    )["session_id"]

    physical = {
        "date": "datetime", "region": "string", "category": "string",
        "grp": "string", "segment": "string", "subcat": "string",
        "sales": "float", "profit": "float", "discount": "float",
    }
    semantic = {
        "date": SemanticType.date, "region": SemanticType.geo,
        "category": SemanticType.dimension, "grp": SemanticType.dimension,
        "segment": SemanticType.dimension, "subcat": SemanticType.dimension,
        "sales": SemanticType.metric, "profit": SemanticType.metric,
        "discount": SemanticType.metric,
    }
    fields = []
    for col in df.columns:
        sem = semantic[col]
        fields.append(FieldProfile(
            name=col,
            physical_type=physical[col],
            semantic_type=sem,
            confidence=Confidence.high,
            confirmed=True,
            is_metric=sem is SemanticType.metric,
            is_date=sem is SemanticType.date,
            cardinality=int(df[col].nunique()),
        ))
    dictionary = DataDictionary(session_id=sid, fields=fields, complete=True)
    store.write_artifact(sid, "dictionary", dictionary.model_dump(mode="json"))

    snapshot_hash = store.save_snapshot(sid, df)
    report = QualityReport(
        session_id=sid, complete=True,
        snapshot_rows=len(df), snapshot_hash=snapshot_hash,
    )
    store.write_artifact(sid, "quality", report.model_dump(mode="json"))
    return store, sid, dictionary, df


def _run_scan_and_signals(tmp_path: Path, df: pd.DataFrame) -> tuple:
    store, sid, dictionary, snapshot = _craft_session(tmp_path, df)
    bundle = plan_scan(dictionary, snapshot)
    store.write_bundle(sid, bundle.model_dump(mode="json"))
    execution = execute_bundle(sid, store, bundle)
    signals = extract_signals(sid, store, bundle, execution, dictionary)
    return store, sid, bundle, execution, signals


def _superstore_df() -> pd.DataFrame:
    regions = ["East", "West", "Central", "South"]
    cats = ["Furniture", "Office Supplies", "Technology"]
    segments = ["Consumer", "Corporate", "Home Office"]
    rows = []
    for i in range(240):
        month = (i // 20) % 12  # 每月 20 行块：各 category×region 每月均有覆盖
        region = regions[i % 4]
        cat = cats[i % 3]
        segment = segments[i % 3]
        subcat = f"sub_{i % 6}"
        # 与 i 近独立的确定性折扣序列（避免与 sales 的线性增长相关）
        discount = [0.0, 0.2, 0.4, 0.1, 0.3, 0.0, 0.15][(i * 5 + 3) % 7]
        base = 500 + i * 5
        if cat == "Furniture":
            sales = 300 + (i % 10) * 40
            profit = 100 - discount * 900 - 250
        elif cat == "Technology":
            sales = base + 400
            profit = sales * 0.30 - discount * sales * 0.7
        else:
            sales = base + 100
            profit = sales * 0.12 - discount * sales * 0.45
        if i in (17, 53, 89):
            profit -= 900  # 极端离群
        rows.append((
            f"2024-{month + 1:02d}-15", region, cat, segment, subcat,
            round(float(discount), 2), round(float(sales), 2), round(float(profit), 2),
        ))
    return pd.DataFrame(
        rows, columns=[
            "date", "region", "category", "segment", "subcat",
            "discount", "sales", "profit",
        ]
    )


# ------------------------------------------------------------ TR-3.1


def test_all_embedded_signals_extracted(tmp_path):
    store, sid, bundle, execution, signals = _run_scan_and_signals(
        tmp_path, _superstore_df()
    )
    types = {s.type for s in signals}

    assert "negative_member" in types   # Furniture 亏损
    assert "divergence" in types       # 量利份额背离
    assert "anomaly" in types          # 极端值
    assert "correlation" in types      # discount~profit 强相关
    assert len(signals) >= 4


def test_negative_member_magnitude_matches_view(tmp_path):
    store, sid, bundle, execution, signals = _run_scan_and_signals(
        tmp_path, _superstore_df()
    )
    neg = next(
        s for s in signals
        if s.type == "negative_member" and s.dimension == "category"
    )

    assert neg.member == "Furniture"
    assert neg.direction == "negative"
    # magnitude 与引用视图中该成员的真实聚合值逐值一致
    vid = neg.scan_view_ids[0]
    df = pd.read_parquet(store.view_dir(sid, vid) / "result.parquet")
    row = df[df["category"] == "Furniture"].iloc[0]
    assert neg.magnitude.value == float(row["value"])
    assert neg.magnitude.value < 0


def test_divergence_gap_recomputable_from_views(tmp_path):
    store, sid, bundle, execution, signals = _run_scan_and_signals(
        tmp_path, _superstore_df()
    )
    div = next(
        s for s in signals
        if s.type == "divergence" and s.dimension == "category"
    )

    assert div.member == "Furniture"
    # 用两个证据视图重算份额缺口，逐值一致
    sales_view = next(
        v for v in bundle.analysis_views
        if v.view_id in div.scan_view_ids and v.metric_fields[0] == "sales"
        and v.dimension_fields[0] == "category"
        and v.type in ("comparison", "breakdown")
    )
    profit_view = next(
        v for v in bundle.analysis_views
        if v.view_id in div.scan_view_ids and v.metric_fields[0] == "profit"
        and v.dimension_fields[0] == "category"
        and v.type in ("comparison", "breakdown")
    )
    sdf = pd.read_parquet(store.view_dir(sid, sales_view.view_id) / "result.parquet")
    pdf = pd.read_parquet(store.view_dir(sid, profit_view.view_id) / "result.parquet")

    def share(df, member):
        total = float(df["value"].sum())
        return float(df.loc[df["category"] == member, "value"].iloc[0]) / total

    gap = (share(pdf, "Furniture") - share(sdf, "Furniture")) * 100.0
    if div.magnitude.gap_pp is not None:
        assert abs(div.magnitude.gap_pp - gap) < 1e-3
    # Furniture 利润为负，而销售占比显著（>10%）：亏损集中在有规模的品类
    assert div.magnitude.value is not None and div.magnitude.value < 0
    sales_share = float(sdf.loc[sdf["category"] == "Furniture", "value"].iloc[0]) / float(
        sdf["value"].sum()
    )
    assert sales_share >= 0.10


def test_correlation_signal_grounded_and_guarded(tmp_path):
    store, sid, bundle, execution, signals = _run_scan_and_signals(
        tmp_path, _superstore_df()
    )
    corr = next(s for s in signals if s.type == "correlation")

    assert corr.direction == "down"  # 折扣越高利润越低
    r = corr.magnitude.coefficient
    assert r is not None and abs(r) >= 0.3
    # r 与视图 summary 一致
    vid = corr.scan_view_ids[0]
    er = next(x for x in execution.views if x.view_id == vid)
    assert er.steps[-1].summary["coefficient"] == r


def test_simpson_signal_extracted(tmp_path):
    # p1：A 组 40 行高率，B 组 10 行低率；
    # p2：A 组 10 行（率改善），B 组 40 行（率改善）→ 整体率反降
    rows = []
    for _ in range(40):
        rows.append(("2024-01-15", "A", 100.0, 80.0))
    for _ in range(10):
        rows.append(("2024-01-15", "B", 100.0, 20.0))
    for _ in range(10):
        rows.append(("2024-02-15", "A", 100.0, 85.0))
    for _ in range(40):
        rows.append(("2024-02-15", "B", 100.0, 25.0))
    df = pd.DataFrame(rows, columns=["date", "grp", "sales", "profit"])

    store, sid, bundle, execution, signals = _run_scan_and_signals(tmp_path, df)
    types = {s.type for s in signals}

    assert "simpson" in types
    simpson = next(s for s in signals if s.type == "simpson")
    assert simpson.dimension == "grp"
    assert simpson.magnitude.gap_pp is not None
    assert simpson.magnitude.gap_pp <= -1.0


# ------------------------------------------------------------ TR-3.2


def test_rank_deterministic_and_topk(tmp_path):
    store, sid, bundle, execution, first = _run_scan_and_signals(
        tmp_path, _superstore_df()
    )
    second = extract_signals(sid, store, bundle, execution,
                             DataDictionary.model_validate(
                                 store.read_artifact(sid, "dictionary")))

    # 同输入两次：ID/类型/分数序列完全一致
    assert [(s.signal_id, s.score) for s in first] == [
        (s.signal_id, s.score) for s in second
    ]
    # signal_id 按排序顺序编号；分数单调不增
    scores = [s.score for s in first]
    assert scores == sorted(scores, reverse=True)
    assert [s.signal_id for s in first] == [
        f"sig_{i:02d}" for i in range(1, len(first) + 1)
    ]

    signal_set = build_signal_set(sid, "run_x", first)
    assert isinstance(signal_set, SignalSet)
    assert len(signal_set.top_signal_ids) == min(3, len(first))
    # top 即排序前 3
    assert signal_set.top_signal_ids == [s.signal_id for s in first[:3]]


def test_no_signal_data_returns_empty(tmp_path):
    # 常量数据：无差异/无负/无异常/无相关
    df = pd.DataFrame({
        "category": ["A", "B", "C"] * 20,
        "sales": [100.0] * 60,
        "profit": [20.0] * 60,
    })
    store, sid, bundle, execution, signals = _run_scan_and_signals(tmp_path, df)

    assert signals == []
    signal_set = build_signal_set(sid, "run_x", signals)
    assert signal_set.signals == [] and signal_set.top_signal_ids == []


def test_score_breakdown_and_view_refs_valid(tmp_path):
    store, sid, bundle, execution, signals = _run_scan_and_signals(
        tmp_path, _superstore_df()
    )
    valid_ids = {v.view_id for v in bundle.analysis_views}
    for s in signals:
        assert 0.0 <= s.score <= 1.0
        assert s.scan_view_ids
        assert all(vid in valid_ids for vid in s.scan_view_ids)
        for part in ("impact", "concentration", "novelty", "support"):
            assert 0.0 <= getattr(s.score_breakdown, part) <= 1.0
