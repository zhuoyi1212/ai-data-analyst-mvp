"""重构 Phase 1：BundlePlanner 规则选择器测试。

覆盖：5 示例数据 4-8 个高价值 View、算子白名单、三类多样性与信息冗余去重、
多指标对照、以及选择器的去重/上限规则（为未来 LLM 选择器固定行为契约）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from app.schemas.bundle import AnalysisView, ViewType
from app.schemas.common import Confidence, SemanticType
from app.schemas.dictionary import DataDictionary, FieldProfile
from app.services.bundle_planner import (
    Candidate,
    CandidateGenerator,
    RuleBundleSelector,
    SelectionContext,
    build_bundle,
)
from app.services.profiler import confirm_fields, generate_dictionary
from app.services.quality_actions import apply_decisions
from app.services.quality_checker import run_quality_checks
from app.services.storage import SessionStore
from scripts.build_e2e_fixtures import DATASET_META
from scripts.generate_sample_data import DATASETS_CONFIG, GOLDEN_PATH

GOLDEN = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["datasets"]

ALLOWED_OPS = {
    "filter", "aggregate", "group_by", "top_n", "share", "time_series",
    "compare_period", "correlation", "outlier_flag", "join_lookup",
    "derive_ratio", "contribution", "rate_decomposition",
}


def _ready_session(name: str, tmp_path: Path):
    """完成上传→语义确认→质量处理，返回 (store, sid, dictionary, snapshot)。"""
    golden = GOLDEN[name]
    medium_col = next(iter(DATASET_META[name]["medium"]))
    store = SessionStore(tmp_path / "store")
    sid = store.create_from_sample(golden["filename"])["session_id"]
    generate_dictionary(sid, store)
    confirm_fields(sid, [{
        "name": medium_col,
        "semantic_type": DATASETS_CONFIG[name]["semantics"][medium_col].value,
        "meaning": "用户确认的字段含义",
    }], store)
    run_quality_checks(sid, store)
    apply_decisions(sid, golden["decisions"], store)
    dictionary = DataDictionary.model_validate(store.read_artifact(sid, "dictionary"))
    return store, sid, dictionary, store.load_snapshot(sid)


@pytest.mark.parametrize("name", list(DATASETS_CONFIG))
def test_bundle_view_count_and_coverage(name: str, tmp_path: Path):
    store, sid, dictionary, snapshot = _ready_session(name, tmp_path)
    bundle = build_bundle(
        dictionary, snapshot, snapshot_rows=len(snapshot),
        title=GOLDEN[name]["filename"],
    )
    views = bundle.analysis_views
    # T07：Seed（presentation）只保留核心 KPI/趋势（必选）与基准拆分
    # （有可加指标 + 合适维度时）：2-5 个，绝不按图表类型凑数量
    presentation = [v for v in views if v.role == "presentation"]
    assert 2 <= len(presentation) <= 5, f"{name} Seed 应为 2-5 个，实际 {len(presentation)}"
    assert any(v.type is ViewType.overview for v in presentation)
    if dictionary.date_fields():
        assert any(v.type is ViewType.trend for v in presentation)
    assert len(views) <= 12, f"{name} 总 View 不应超过 12，实际 {len(views)}"
    # view_id 唯一、按序编号
    assert [v.view_id for v in views] == [f"view_{i:02d}" for i in range(1, len(views) + 1)]
    # 所有 View 都有可解释的选择理由（可解释性）
    assert all(v.selection_reason and v.source.startswith("rule") for v in views)


@pytest.mark.parametrize("name", list(DATASETS_CONFIG))
def test_bundle_plans_use_whitelist_ops_and_valid_fields(name: str, tmp_path: Path):
    _, _, dictionary, snapshot = _ready_session(name, tmp_path)
    bundle = build_bundle(dictionary, snapshot, snapshot_rows=len(snapshot), title=name)
    cols = set(snapshot.columns)
    for v in bundle.analysis_views:
        terminal = v.plan.steps[-1].op
        assert terminal in ALLOWED_OPS, f"{v.view_id} 使用白名单外算子 {terminal}"
        assert v.chart is not None
        for f in v.metric_fields + v.dimension_fields:
            assert f in cols, f"{v.view_id} 引用了不存在的字段 {f}"


@pytest.mark.parametrize("name", list(DATASETS_CONFIG))
def test_bundle_diversity_and_no_redundancy(name: str, tmp_path: Path):
    _, _, dictionary, snapshot = _ready_session(name, tmp_path)
    bundle = build_bundle(dictionary, snapshot, snapshot_rows=len(snapshot), title=name)
    views = bundle.analysis_views

    dist_views = [v for v in views if v.type in
                  (ViewType.comparison, ViewType.breakdown, ViewType.ranking)]

    # 维度多样性：分布视角可以「同维度跨指标对照」，但同一维度最多出现 2 次，
    # 且出现 2 次时两次的指标必须不同（禁止同维同指标仅换分析类型）
    dims_used = [v.dimension_fields[0] for v in dist_views]
    dim_count: dict[str, int] = {}
    for d in dims_used:
        dim_count[d] = dim_count.get(d, 0) + 1
    assert max(dim_count.values(), default=0) <= 2, f"{name} 同维度出现超过 2 次：{dim_count}"
    for dim, n in dim_count.items():
        if n == 2:
            ms = {v.metric_fields[0] for v in dist_views
                  if v.dimension_fields[0] == dim}
            assert len(ms) == 2, f"{name} 维度「{dim}」重复但指标未形成对照：{ms}"

    # 信息冗余铁律：同一 (dimension, metric) 不得重复（即使分析类型不同）
    pairs = [(v.dimension_fields[0], v.metric_fields[0]) for v in dist_views]
    assert len(pairs) == len(set(pairs)), f"{name} 存在同维同指标重复视角：{pairs}"

    # profitability（派生比率）是同维度允许的第 3 视角：
    # 必须锚定已有分布视角的维度；同维合计 ≤3；同 (dim,分子,分母) 不得重复
    prof_views = [v for v in views if v.type == ViewType.profitability]
    all_dim_views = dist_views + prof_views
    total_count: dict[str, int] = {}
    for v in all_dim_views:
        d = v.dimension_fields[0]
        total_count[d] = total_count.get(d, 0) + 1
    assert max(total_count.values(), default=0) <= 3
    for v in prof_views:
        assert v.plan.steps[-1].op == "derive_ratio"
        dim = v.dimension_fields[0]
        assert dim_count.get(dim, 0) >= 1, f"{name} 利润率视角未锚定结构维度：{dim}"
    ratio_ids = [
        (v.dimension_fields[0], v.metric_fields[0], v.metric_fields[1])
        for v in prof_views
    ]
    assert len(ratio_ids) == len(set(ratio_ids)), f"{name} 派生比率视角重复：{ratio_ids}"


@pytest.mark.parametrize("name", list(DATASETS_CONFIG))
def test_bundle_includes_trend_when_date_exists(name: str, tmp_path: Path):
    _, _, dictionary, snapshot = _ready_session(name, tmp_path)
    bundle = build_bundle(dictionary, snapshot, snapshot_rows=len(snapshot), title=name)
    if dictionary.date_fields():
        assert any(v.type == ViewType.trend for v in bundle.analysis_views)
    assert any(v.type == ViewType.overview for v in bundle.analysis_views)


def test_multi_metric_overview_contrast(tmp_path: Path):
    """存在 Sales + Profit 两个 P&L 指标时，必须同时产出两个总览（为跨 View 洞察打底）。"""
    dates = pd.date_range("2024-01-01", periods=90, freq="D")
    df = pd.DataFrame({
        "日期": dates.repeat(2)[:180],
        "区域": (["华东", "华南", "华北"] * 60)[:180],
        "Sales": [100.0 + i % 50 for i in range(180)],
        "Profit": [20.0 - (i % 30) for i in range(180)],
    })

    def prof(name, sem, card):
        return FieldProfile(
            name=name, physical_type="float", semantic_type=sem, meaning=name,
            confidence=Confidence.high, confirmed=True, cardinality=card, is_metric=True,
        )
    dictionary = DataDictionary(session_id="s", fields=[
        FieldProfile(name="日期", physical_type="datetime", semantic_type=SemanticType.date,
                     meaning="日期", confidence=Confidence.high, confirmed=True,
                     cardinality=90, is_date=True),
        FieldProfile(name="区域", physical_type="string", semantic_type=SemanticType.dimension,
                     meaning="区域", confidence=Confidence.high, confirmed=True, cardinality=3),
        prof("Sales", SemanticType.metric, 180),
        prof("Profit", SemanticType.metric, 180),
    ], complete=True)

    bundle = build_bundle(dictionary, df, snapshot_rows=180, title="synthetic")
    overviews = [v for v in bundle.analysis_views if v.type == ViewType.overview]
    metrics = {v.metric_fields[0] for v in overviews}
    assert {"Sales", "Profit"} <= metrics
    assert bundle.primary_metrics[:2] == ["Sales", "Profit"]

    # 跨指标对照：必须存在某个维度同时承载 Sales 与 Profit 两个分布视角
    # （同维度不同指标，为跨 View 的量利背离洞察打底）
    dist = [v for v in bundle.analysis_views
            if v.type in (ViewType.comparison, ViewType.breakdown, ViewType.ranking)]
    dim_metrics: dict[str, set[str]] = {}
    for v in dist:
        dim_metrics.setdefault(v.dimension_fields[0], set()).add(v.metric_fields[0])
    assert any({"Sales", "Profit"} <= ms for ms in dim_metrics.values()), \
        f"缺少 Sales/Profit 同维度对照：{dim_metrics}"


def test_profitability_replaces_ranking_and_uses_derive_ratio():
    """Sales+Profit 共存：profitability 入选并替换主指标 ranking 槽位；比率走 derive_ratio。"""
    dates = pd.date_range("2024-01-01", periods=90, freq="D")
    df = pd.DataFrame({
        "日期": dates.repeat(2)[:180],
        "区域": (["华东", "华南", "华北"] * 60)[:180],
        "Sales": [100.0 + i % 50 for i in range(180)],
        "Profit": [20.0 - (i % 30) for i in range(180)],
    })
    dictionary = DataDictionary(session_id="s", fields=[
        FieldProfile(name="日期", physical_type="datetime", semantic_type=SemanticType.date,
                     meaning="日期", confidence=Confidence.high, confirmed=True,
                     cardinality=90, is_date=True),
        FieldProfile(name="区域", physical_type="string", semantic_type=SemanticType.dimension,
                     meaning="区域", confidence=Confidence.high, confirmed=True, cardinality=3),
        FieldProfile(name="Sales", physical_type="float", semantic_type=SemanticType.metric,
                     meaning="销售额", confidence=Confidence.high, confirmed=True,
                     cardinality=180, is_metric=True),
        FieldProfile(name="Profit", physical_type="float", semantic_type=SemanticType.metric,
                     meaning="利润", confidence=Confidence.high, confirmed=True,
                     cardinality=180, is_metric=True),
    ], complete=True)

    bundle = build_bundle(dictionary, df, snapshot_rows=180, title="synthetic")
    prof = [v for v in bundle.analysis_views if v.type == ViewType.profitability]
    assert len(prof) == 1
    v = prof[0]
    assert v.metric_fields == ["Profit", "Sales"]
    assert v.dimension_fields == ["区域"]
    step = v.plan.steps[-1]
    assert step.op == "derive_ratio"
    assert step.params.numerator == "Profit"
    assert step.params.denominator == "Sales"
    assert step.params.dimension == "区域"
    # 槽位替换：profitability 在场时主指标不再占 ranking
    assert not any(vv.type == ViewType.ranking and vv.metric_fields[0] == "Sales"
                   for vv in bundle.analysis_views)
    # 顺序契约：结构对照（Profit 对比）在利润率之前
    types = [vv.type for vv in bundle.analysis_views]
    assert types.index(ViewType.profitability) > types.index(ViewType.comparison)


def _cand(family, vt, group=2) -> Candidate:
    dim = (family[1],) if len(family) >= 2 else ()
    metric = (family[2],) if len(family) >= 3 else ("m0",)
    return Candidate(
        type=vt, title="t", question="q", op="group_by", hint={}, fields=[],
        metric_fields=list(metric), dimension_fields=list(dim),
        family_key=family, reason="r", priority_group=group,
    )


def _metric_prof(name: str) -> FieldProfile:
    return FieldProfile(
        name=name, physical_type="float", semantic_type=SemanticType.metric,
        meaning=name, confidence=Confidence.high, confirmed=True,
        cardinality=100, is_metric=True,
    )


def test_selector_dedup_family_and_cap():
    """T07：同 family_key 只取一个（含 Seed 与证据任务之间）；总数不超过 max_views。"""
    ctx = SelectionContext(metrics=[_metric_prof("金额")], dimensions=[], snapshot_rows=100)
    cands = [
        Candidate(type=ViewType.overview, title="t", question="q", op="aggregate",
                  hint={}, fields=[], metric_fields=["金额"], dimension_fields=[],
                  family_key=("overview", "金额"), reason="r", priority_group=0),
        _cand(("dist", "区域", "金额"), ViewType.comparison),
        _cand(("dist", "区域", "金额"), ViewType.breakdown),  # 同 family，二选一
        _cand(("dist", "城市", "金额"), ViewType.ranking),
    ]
    chosen = RuleBundleSelector().select(cands, ctx)
    keys = [c.family_key for c in chosen]
    assert len(keys) == len(set(keys))
    assert ("dist", "区域", "金额") in keys
    assert len(chosen) == 3
    assert len({c.type for c in chosen}) == 3
    # Seed（overview + breakdown）默认展示；额外 ranking 为隐藏证据任务
    assert chosen[0].role == "presentation"
    assert chosen[-1].role == "computation"


def test_selector_respects_max_views():
    ctx = SelectionContext(
        metrics=[_metric_prof("金额甲"), _metric_prof("金额乙")],
        dimensions=[], snapshot_rows=100, max_views=2,
    )
    cands = [
        Candidate(type=ViewType.overview, title="t", question="q", op="aggregate",
                  hint={}, fields=[], metric_fields=["金额甲"], dimension_fields=[],
                  family_key=("overview", "金额甲"), reason="r", priority_group=0),
        Candidate(type=ViewType.overview, title="t", question="q", op="aggregate",
                  hint={}, fields=[], metric_fields=["金额乙"], dimension_fields=[],
                  family_key=("overview", "金额乙"), reason="r", priority_group=0),
    ]
    chosen = RuleBundleSelector().select(cands, ctx)
    assert len(chosen) == 2


def test_candidate_generator_emits_redundant_pool(tmp_path: Path):
    """候选生成阶段故意冗余（同维同指标的对比/占比都生成），去重是 Selection 的职责。"""
    _, _, dictionary, snapshot = _ready_session("sales_orders", tmp_path)
    cands = CandidateGenerator().generate(dictionary, snapshot)
    families = [c.family_key for c in cands]
    # 候选池本身允许冗余
    assert len(families) > len(set(families))
    # 但经过选择器后必须无冗余
    bundle = build_bundle(dictionary, snapshot, snapshot_rows=len(snapshot), title="x")
    dist = [v for v in bundle.analysis_views
            if v.type in (ViewType.comparison, ViewType.breakdown, ViewType.ranking)]
    seen = {(v.dimension_fields[0], v.metric_fields[0]) for v in dist}
    assert len(seen) == len(dist)
