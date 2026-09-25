"""DashboardArtifact 合成器（重构 Phase 2，规则版）。

输入：已落盘的 AnalysisBundle + BundleExecutionResult + 各 View result.parquet；
输出：DashboardArtifact（KPI / 五段布局 / Findings / 全局筛选）。

红线：
- 数值唯一来源是引擎算子：View 数值读 result.parquet，派生比率走 derive_ratio probe；
- 合成器只做读取、末两期环比这类确定性比较和文案组装，不新造业务数字；
- probe 全生命周期异常收敛（partial success），总数 ≤3。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.schemas.bundle import (
    AnalysisBundle,
    AnalysisView,
    BundleExecutionResult,
    ViewStatus,
    ViewType,
)
from app.schemas.dashboard import (
    ChartSpec,
    DashboardArtifact,
    DashboardSection,
    FilterDefinition,
    KPI,
)
from app.schemas.dictionary import DataDictionary
from app.services.derived_metrics import detect_derived_metrics
from app.services.executor import execute_plan
from app.services.offline_fallback import build_plan
from app.services.storage import SessionStore

MAX_PROBES = 3

_GRAN_CHANGE = {
    "month": "mom",
    "week": "wow",
    "year": "yoy",
    "day": "previous_period",
    "quarter": "previous_period",
}

_SECTIONS: list[tuple[str, str, set[ViewType]]] = [
    ("overview", "总体表现", {ViewType.overview}),
    ("trend", "时间趋势", {ViewType.trend}),
    ("structure", "结构分布", {ViewType.breakdown, ViewType.comparison}),
    ("diagnosis", "诊断分析", {
        ViewType.profitability, ViewType.relationship, ViewType.anomaly,
    }),
    ("detail", "明细与排名", {ViewType.ranking}),
]


# ----------------------------------------------------------------- 探针

@dataclass
class ProbeResult:
    df: pd.DataFrame
    records: list
    elapsed_ms: float


class ProbeRunner:
    """洞察探针执行器：预算 ≤3，异常全收敛，产物隔离在 bundle/probes/。"""

    def __init__(self, session_id: str, store: SessionStore, snapshot: pd.DataFrame):
        self.session_id = session_id
        self.store = store
        self.snapshot = snapshot
        self.used = 0

    def run(self, probe_id: str, plan) -> ProbeResult | None:
        if self.used >= MAX_PROBES:
            return None
        self.used += 1
        try:
            df, records, elapsed_ms = execute_plan(self.snapshot, plan)
            self.store.write_probe_plan(
                self.session_id, probe_id, plan.model_dump(mode="json")
            )
            self.store.save_probe_result(self.session_id, probe_id, df)
            self.store.write_probe_ledger(self.session_id, probe_id, {
                "probe_id": probe_id,
                "elapsed_ms": elapsed_ms,
                "steps": [r.model_dump(mode="json") for r in records],
            })
            return ProbeResult(df=df, records=records, elapsed_ms=elapsed_ms)
        except Exception:  # noqa: BLE001 —— probe 失败不阻断 Dashboard 合成
            return None


# ----------------------------------------------------------------- KPI

def _view_result(store: SessionStore, sid: str, view: AnalysisView) -> pd.DataFrame:
    return pd.read_parquet(
        store.view_dir(sid, view.view_id) / "result.parquet"
    )


def _tail_change(df: pd.DataFrame) -> float | None:
    """末两期确定性环比（小数）；末期或次期为空/分母 0 时不造数。"""
    if len(df) < 2 or "value" not in df.columns:
        return None
    last2 = df["value"].tail(2)
    prev, last = float(last2.iloc[0]), float(last2.iloc[1])
    if pd.isna(prev) or pd.isna(last) or prev == 0:
        return None
    return (last - prev) / abs(prev)


def _build_kpis(
    sid: str,
    store: SessionStore,
    bundle: AnalysisBundle,
    views: dict[str, AnalysisView],
    execution: BundleExecutionResult,
    dictionary: DataDictionary,
    runner: ProbeRunner,
) -> list[KPI]:
    success = {
        r.view_id: r for r in execution.views if r.status == ViewStatus.success
    }
    overviews = [
        views[vid] for vid in [v.view_id for v in bundle.analysis_views]
        if vid in views and vid in success and views[vid].type is ViewType.overview
    ][:2]
    trends = [
        views[vid] for vid in success
        if views[vid].type is ViewType.trend
    ]

    kpis: list[KPI] = []
    for v in overviews:
        df = _view_result(store, sid, v)
        value = float(df["value"].iloc[0]) if len(df) and pd.notna(df["value"].iloc[0]) else None
        metric = v.metric_fields[0] if v.metric_fields else v.title
        change = change_type = None
        tv = next((t for t in trends if t.metric_fields and t.metric_fields[0] == metric), None)
        if tv is not None:
            tdf = _view_result(store, sid, tv)
            change = _tail_change(tdf)
            gran = tv.plan.steps[-1].params.granularity.value
            change_type = _GRAN_CHANGE.get(gran)
        kpis.append(KPI(
            label=metric, value=value,
            change=change, change_type=change_type, unit="",
        ))

    # 派生比率 KPI（第一版：利润率）——无轴单值 probe，口径 Σnum/Σden
    for i, spec in enumerate(detect_derived_metrics(dictionary)[:1]):
        plan = build_plan(
            f"整体「{spec.label}」（Σ{spec.numerator}÷Σ{spec.denominator}）是多少？",
            "derive_ratio",
            {"numerator": spec.numerator, "denominator": spec.denominator},
            [spec.numerator, spec.denominator],
        )
        probe = runner.run(f"probe_kpi_{spec.key}", plan)
        ratio = None
        if probe is not None and len(probe.df) and "value" in probe.df.columns:
            raw = probe.df["value"].iloc[0]
            ratio = float(raw) if pd.notna(raw) else None
        kpis.append(KPI(label=spec.label, value=ratio, unit=spec.unit))

    return kpis


# ----------------------------------------------------------------- ChartSpec

def build_chart_spec(view: AnalysisView) -> ChartSpec | None:
    """由 view.type + 末端算子参数确定性生成 Phase 3 图表契约；KPI 卡不出图。"""
    p = view.plan.steps[-1].params
    op = view.plan.steps[-1].op
    if op in ("aggregate",):
        return None
    if op == "time_series":
        return ChartSpec(
            type=view.chart, title=view.title,
            x_field=p.date_column, y_fields=[p.metric], metric=p.metric,
        )
    if op in ("group_by", "top_n"):
        return ChartSpec(
            type=view.chart, title=view.title,
            x_field=p.dimension, y_fields=["value"], dimension=p.dimension,
            metric=p.metric, interactive=True,
        )
    if op == "share":
        return ChartSpec(
            type=view.chart, title=view.title,
            x_field=p.dimension, y_fields=["value", "share"],
            dimension=p.dimension, metric=p.metric, interactive=True,
        )
    if op == "derive_ratio":
        measure = f"{p.numerator}/{p.denominator}"
        return ChartSpec(
            type=view.chart, title=view.title,
            x_field=p.dimension, y_fields=["value"], dimension=p.dimension,
            metric=measure, interactive=True,
        )
    if op == "correlation":
        return ChartSpec(
            type=view.chart, title=view.title,
            x_field=p.column_x, y_fields=[p.column_y],
            metric=f"{p.column_x}~{p.column_y}",
        )
    if op == "outlier_flag":
        return ChartSpec(
            type=view.chart, title=view.title,
            y_fields=[p.column], metric=p.column,
        )
    return None


# ----------------------------------------------------------------- 布局/筛选

def _build_sections(
    bundle: AnalysisBundle, success_ids: set[str]
) -> list[DashboardSection]:
    sections: list[DashboardSection] = []
    for sid_, title, types in _SECTIONS:
        ids = [
            v.view_id for v in bundle.analysis_views
            if v.view_id in success_ids and v.type in types
        ]
        sections.append(DashboardSection(section_id=sid_, title=title, view_ids=ids))
    return sections


def _build_filters(
    bundle: AnalysisBundle, dictionary: DataDictionary, snapshot: pd.DataFrame
) -> list[FilterDefinition]:
    cards = {f.name: f for f in dictionary.fields}
    out: list[FilterDefinition] = []
    for col in bundle.primary_dimensions:
        f = cards.get(col)
        if f is None or not (1 < (f.cardinality or 0) <= 30):
            continue
        if col not in snapshot.columns:
            continue
        members = (
            snapshot[col].dropna().astype(str).drop_duplicates().sort_values().tolist()
        )
        out.append(FilterDefinition(column=col, label=col, members=members[:30]))
    return out


# ----------------------------------------------------------------- 主编排

def synthesize_dashboard(
    session_id: str,
    store: SessionStore,
    *,
    findings: list | None = None,
) -> DashboardArtifact:
    """读取 Bundle + 执行结果 → 合成并落盘 DashboardArtifact。

    findings 可由 dashboard_insight 在外部预算内注入；默认不产出（Step 7 接入）。
    """
    # 延迟导入避免模块循环
    from app.services.dashboard_insight import detect_findings

    bundle = AnalysisBundle.model_validate(store.read_bundle(session_id))
    execution = BundleExecutionResult.model_validate(
        store.read_bundle_execution(session_id)
    )
    dictionary = DataDictionary.model_validate(
        store.read_artifact(session_id, "dictionary")
    )
    snapshot = store.load_snapshot(session_id)
    views = {v.view_id: v for v in bundle.analysis_views}
    success_ids = {
        r.view_id for r in execution.views if r.status == ViewStatus.success
    }

    runner = ProbeRunner(session_id, store, snapshot)
    kpis = _build_kpis(session_id, store, bundle, views, execution, dictionary, runner)

    # ChartSpec 随 view 级目录落盘（artifact 只持有 view_id 索引）
    for vid in success_ids:
        spec = build_chart_spec(views[vid])
        if spec is not None:
            store.write_view_chart_spec(
                session_id, vid, spec.model_dump(mode="json")
            )

    sections = _build_sections(bundle, success_ids)
    filters = _build_filters(bundle, dictionary, snapshot)

    if findings is None:
        findings = detect_findings(
            session_id, store, bundle, execution, dictionary, runner
        )
    risks = [f for f in findings if f.type == "risk"]

    artifact = DashboardArtifact(
        bundle_id=bundle.bundle_id,
        title=bundle.title,
        kpis=kpis,
        sections=sections,
        findings=findings,
        risks=risks,
        global_filters=filters,
    )
    store.write_dashboard(session_id, artifact.model_dump(mode="json"))
    return artifact
