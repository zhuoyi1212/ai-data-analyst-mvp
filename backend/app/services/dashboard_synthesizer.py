"""DashboardArtifact 合成器（重构 Phase 2，规则版）。

输入：已落盘的 AnalysisBundle + BundleExecutionResult + 各 View result.parquet；
输出：DashboardArtifact（KPI / 五段布局 / Findings / 全局筛选）。

红线：
- 数值唯一来源是引擎算子：View 数值读 result.parquet，派生比率走 derive_ratio probe；
- 合成器只做读取、末两期环比这类确定性比较和文案组装，不新造业务数字；
- probe 全生命周期异常收敛（partial success），总数 ≤3。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.schemas.bundle import (
    AnalysisBundle,
    AnalysisView,
    BundleExecutionResult,
    ViewExecutionResult,
    ViewStatus,
    ViewType,
)
from app.schemas.dashboard import (
    AppliedFilter,
    ChartSpec,
    DashboardArtifact,
    DashboardScope,
    DashboardSection,
    FailedView,
    FilterDefinition,
    KPI,
    ViewCard,
)
from app.schemas.dictionary import DataDictionary
from app.schemas.auto import ChainSet
from app.services.bundle_planner import (
    apply_scope_filters,
    normalize_scope,
)
from app.services.layout_composer import compose_layout
from app.services.derived_metrics import detect_derived_metrics
from app.services.executor import execute_plan
from app.services.metric_spec import effective_metric_spec
from app.services.offline_fallback import build_plan
from app.services.period_compare import PeriodCompareError, period_comparison
from app.services.storage import SessionStore, StaleRunError, StorageError
from app.services.value_filter import ValueAssessment, filter_run
from app.services.view_data import build_envelope
from app.services.field_labels import field_label, humanize_text
from app.services.card_interpretation import interpret_view

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
        ViewType.contribution, ViewType.rate_shift,
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
    """洞察探针执行器：预算 ≤3，异常全收敛，产物隔离在 runs/{run_id}/probes/。"""

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
            # T04：probe 与 View 同门禁——零行/全空结果不可消费（不拿 None/0 当事实）
            if df is None or len(df.index) == 0:
                return None
            numeric = df.select_dtypes(include=["number"])
            if len(numeric.columns) and numeric.notna().sum().sum() == 0:
                return None
            self.store.write_probe_plan(
                self.session_id, probe_id, plan.model_dump(mode="json")
            )
            self.store.save_probe_result(self.session_id, probe_id, df)
            self.store.write_probe_ledger(self.session_id, probe_id, {
                "probe_id": probe_id,
                "elapsed_ms": elapsed_ms,
                "consumable": True,
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


def consumable_results(
    execution: BundleExecutionResult,
) -> dict[str, ViewExecutionResult]:
    """KPI/Chart/Finding/probe 共享的唯一消费门禁（T04）。"""
    return {r.view_id: r for r in execution.views if r.consumable}


def _build_kpis(
    sid: str,
    store: SessionStore,
    bundle: AnalysisBundle,
    views: dict[str, AnalysisView],
    execution: BundleExecutionResult,
    dictionary: DataDictionary,
    runner: ProbeRunner,
    scope_snapshot: pd.DataFrame,
) -> list[KPI]:
    ok = consumable_results(execution)
    overviews = [
        views[vid] for vid in [v.view_id for v in bundle.analysis_views]
        if vid in views and vid in ok and views[vid].type is ViewType.overview
    ][:2]
    trends = [views[vid] for vid in ok if views[vid].type is ViewType.trend]
    field_map = {f.name: f for f in dictionary.fields}

    kpis: list[KPI] = []
    for v in overviews:
        df = _view_result(store, sid, v)
        if not len(df) or pd.isna(df["value"].iloc[0]):
            continue  # T04：无数据不产生 KPI，绝不显示 0
        value = float(df["value"].iloc[0])
        metric = v.metric_fields[0] if v.metric_fields else v.title
        spec = effective_metric_spec(field_map.get(metric)) if metric in field_map else None
        is_rate = bool(spec and not spec.additive and (spec.unit == "%" or spec.denominator))
        change = change_type = delta = status = hint = None
        tv = next((t for t in trends if t.metric_fields and t.metric_fields[0] == metric), None)
        if tv is not None:
            # T05：在快照上走等长窗口同环比，残缺月用 MTD 对齐，
            # 不直接比较趋势图最后两个（可能残缺的）聚合桶。
            tparams = tv.plan.steps[-1].params
            gran = tparams.granularity.value
            mode = {"month": "mom", "week": "wow", "year": "yoy"}.get(gran, "mom")
            try:
                pc = period_comparison(
                    scope_snapshot, tparams.date_column, metric,
                    tparams.func.value, mode, is_rate=is_rate,
                )
            except PeriodCompareError:
                pc = None
            if pc is not None:
                change = (
                    pc["growth_pct"] / 100.0
                    if pc["growth_pct"] is not None else None
                )
                delta = pc["delta"]
                status = pc.get("status") or ""
                change_type = _GRAN_CHANGE.get(gran)
                if pc["completeness"] != "complete":
                    hint = "当期未结束，按截至日等长窗口比较"
        kpis.append(KPI(
            label=metric, value=value,
            change=change, change_type=change_type,
            change_delta=delta, change_status=status or "",
            change_hint=hint or "", unit="",
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
        kpis.append(KPI(
            label=spec.label, value=ratio, unit=spec.unit,
            change_status="" if ratio is not None else "分母为零，不可计算",
        ))

    return kpis


# ----------------------------------------------------------------- ChartSpec

def build_chart_spec(view: AnalysisView) -> ChartSpec | None:
    """由 view.type + 末端算子参数确定性生成 Phase 3 图表契约；KPI 卡不出图。"""
    p = view.plan.steps[-1].params
    op = view.plan.steps[-1].op
    if op in ("aggregate",):
        return None
    if op == "time_series":
        # 反例 3 修复：time_series 算子实际输出列为 {日期列, value}，
        # ChartSpec 必须命中输出 schema，不能写指标原名。
        return ChartSpec(
            type=view.chart, title=view.title,
            x_field=p.date_column, y_fields=["value"], metric=p.metric,
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
    """T07：sections 只收 presentation 且可消费的视图；
    computation 证据任务默认不占布局（隐藏后仍可按需返回）。"""
    sections: list[DashboardSection] = []
    for sid_, title, types in _SECTIONS:
        ids = [
            v.view_id for v in bundle.analysis_views
            if v.view_id in success_ids
            and v.type in types
            and v.role == "presentation"
        ]
        sections.append(DashboardSection(section_id=sid_, title=title, view_ids=ids))
    return sections


def _build_filters(
    bundle: AnalysisBundle, dictionary: DataDictionary, snapshot: pd.DataFrame,
    selected_by_col: dict[str, list[str]],
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
        out.append(FilterDefinition(
            column=col, label=col, members=members[:30],
            selected_values=selected_by_col.get(col),
        ))
    return out


# ----------------------------------------------------------------- T06 视图字典

def validate_chart_fields(spec: ChartSpec, columns) -> None:
    """T06 验收：ChartSpec 的 x/y 字段必须存在于输出 schema，错配拒绝。

    build_chart_spec 与算子输出同源，正常不会错配；本门禁是显式契约闸，
    任何字段漂移在此阻断合成，而不是让浏览器拿到无法渲染的坏图。
    """
    names = {str(c) for c in columns}
    cols_hint = "、".join(sorted(names))
    if spec.x_field is not None and spec.x_field not in names:
        raise ValueError(
            f"图表契约字段错配：x_field「{spec.x_field}」不在视图输出列（{cols_hint}）中，"
            "请重新生成分析蓝图。"
        )
    for y in spec.y_fields:
        if y not in names:
            raise ValueError(
                f"图表契约字段错配：y_field「{y}」不在视图输出列（{cols_hint}）中，"
                "请重新生成分析蓝图。"
            )


def _section_for_type(t: ViewType) -> str | None:
    for section_id, _, types in _SECTIONS:
        if t in types:
            return section_id
    return None


def _build_view_cards(
    sid: str,
    store: SessionStore,
    bundle: AnalysisBundle,
    views: dict[str, AnalysisView],
    execution: BundleExecutionResult,
    run_id: str,
    specs: dict[str, ChartSpec],
    assessments: dict[str, ValueAssessment],
    dictionary: DataDictionary,
) -> dict[str, ViewCard]:
    """视图字典：每个 View 一卡自足（图表 + 数据 + 口径 + 状态）。

    T07：computation 证据任务照常产出自足卡片，但 default_hidden=True；
    presentation 视图永远默认可见。阴性结果（弱 r/无异常）随卡片保留可返回。
    """
    results = {r.view_id: r for r in execution.views}
    cards: dict[str, ViewCard] = {}
    for view in bundle.analysis_views:
        er = results.get(view.view_id)
        assess = assessments.get(view.view_id)
        common = dict(
            view_id=view.view_id, title=humanize_text(view.title),
            question=view.question,
            type=view.type, section_id=_section_for_type(view.type),
            metric_label=view.metric_fields[0] if view.metric_fields else "",
            role=view.role,
            value_scores=assess.scores if assess else {},
            hide_reasons=assess.hide_reasons if assess else [],
            default_hidden=(
                assess.default_hidden
                if assess else view.role == "computation"
            ),
        )
        if er is None or er.status == ViewStatus.failed:
            reason = er.reason if er is not None else "该视角未返回执行结果。"
            cards[view.view_id] = ViewCard(
                **common, status=ViewStatus.failed, validity="unknown",
                consumable=False, reason=reason,
            )
            continue
        if not er.consumable:
            cards[view.view_id] = ViewCard(
                **common, status=er.status, validity=er.validity,
                consumable=False, reason="该视角无有效数据，不进入展示。",
                checks=er.checks,
            )
            continue
        df = _view_result(store, sid, view)
        data_ref = f"/sessions/{sid}/runs/{run_id}/views/{view.view_id}/rows"

        # 输出列中文名：value/share 锚定视图主指标；其余列查词典/字典 meaning
        metric_context = view.metric_fields[0] if view.metric_fields else None

        def _col_label(col: str) -> str:
            if col in ("value", "share"):
                return field_label(col, metric_context=metric_context)
            fp = dictionary.field(col)
            return field_label(
                col, meaning=fp.meaning if fp is not None else None
            )

        column_labels = {str(col): _col_label(str(col)) for col in df.columns}
        interpretation = interpret_view(
            view_type=view.type.value, df=df,
            metric_fields=view.metric_fields,
            dimension_fields=view.dimension_fields,
            title_hint=view.title,
        )
        cards[view.view_id] = ViewCard(
            **common, chart_spec=specs.get(view.view_id),
            data=build_envelope(
                view.type.value, df, data_ref, column_labels
            ),
            interpretation=interpretation,
            status=er.status, validity=er.validity, consumable=True,
            checks=er.checks,
        )
    return cards


# ----------------------------------------------------------------- 深挖探针卡

def _build_probe_cards(
    sid: str, store: SessionStore, run_id: str, chain_set: Any
) -> dict[str, ViewCard]:
    """深挖链探针 → 与 view 同构的自足卡片（role=computation，默认隐藏）。

    每个 probe 以 group_by 结果为代表（维度+value，与 bar 图契约一致）；
    layout_composer 再挑选高优先级探针置为可见 diagnostic。
    """
    cards: dict[str, ViewCard] = {}
    seen: set[str] = set()
    base = store.run_dir(sid, run_id) / "diagnostic" / "probes"
    for chain in chain_set.chains:
        for dv in chain.diagnostic_views:
            pid = dv.probe_id
            if pid in seen:
                continue
            seen.add(pid)
            pdir = base / pid
            rpath = pdir / "result_group_by.parquet"
            ppath = pdir / "plan_group_by.json"
            if not rpath.exists() or not ppath.exists():
                continue
            df = pd.read_parquet(rpath)
            if not len(df) or "value" not in df.columns:
                continue
            params = json.loads(ppath.read_text())["steps"][-1]["params"]
            dim = params["dimension"]
            metric = params.get("metric") or ""
            dim_cn = field_label(dim)
            spec = ChartSpec(
                type="bar", title=f"{dim_cn} 分组对比",
                x_field=dim, y_fields=["value"], dimension=dim,
                metric=metric, interactive=True,
            )
            data_ref = (
                f"/sessions/{sid}/runs/{run_id}"
                f"/diagnostic/probes/{pid}/rows"
            )
            column_labels = {
                dim: dim_cn,
                "value": field_label("value", metric_context=metric or None),
            }
            cards[pid] = ViewCard(
                view_id=pid, ref_type="probe",
                title=f"{dim_cn} 分组对比",
                question=f"按 {dim_cn} 分组对比",
                type=ViewType.breakdown, section_id="diagnosis",
                chart_spec=spec,
                data=build_envelope(
                    "aggregate", df, data_ref, column_labels
                ),
                interpretation=interpret_view(
                    view_type="comparison", df=df,
                    metric_fields=[metric] if metric else [],
                    dimension_fields=[dim],
                ),
                status=ViewStatus.success, validity="pass", consumable=True,
                metric_label=metric, role="computation",
                default_hidden=True,
                hide_reasons=["深挖链探针，默认折叠"],
            )
    return cards


# ----------------------------------------------------------------- 主编排

def _verify_run_fingerprints(session_id: str, store: SessionStore):
    """T01：合成前校验运行版本与当前快照/字典/执行结果一致，错配即 stale。

    禁止只判文件是否存在——快照被重做、字典口径变更、Bundle 重规划都会使
    旧执行结果成为错误版本。
    """
    manifest = store.run_manifest(session_id)
    session_dir = store.session_dir(session_id)
    snap_path = session_dir / "snapshot.parquet"
    dict_path = session_dir / "dictionary.json"
    actual_snapshot = hashlib.sha256(snap_path.read_bytes()).hexdigest()
    if actual_snapshot != manifest.snapshot_hash:
        raise StaleRunError(
            "数据快照已变化，旧执行结果与当前数据不是同一版本，请重新生成并执行分析蓝图。"
        )
    actual_dict = hashlib.sha256(dict_path.read_bytes()).hexdigest()
    if actual_dict != manifest.dictionary_hash:
        raise StaleRunError(
            "语义字典/指标口径已变化，旧执行结果失效，请重新生成并执行分析蓝图。"
        )
    if not (store.run_dir(session_id, manifest.run_id) / "execution.json").exists():
        raise StaleRunError("当前分析蓝图尚未执行，请先执行后再合成仪表盘。")
    return manifest


def synthesize_dashboard(
    session_id: str,
    store: SessionStore,
    *,
    findings: list | None = None,
    force: bool = False,
) -> DashboardArtifact:
    """读取当前运行的 Bundle + 执行结果 → 合成并原子发布 DashboardArtifact。

    - 同运行重复合成幂等：已发布则直接读回，不重置 probe 预算（T01）；
    - 版本指纹不匹配一律 StaleRunError（路由映射 409），不混版本（T01）；
    - KPI/Chart/Finding 共享 consumable 门禁（T04）；
    - findings 可由外部在预算内注入；默认走 detect_findings。
    """
    # 延迟导入避免模块循环
    from app.services.dashboard_insight import detect_findings

    manifest = _verify_run_fingerprints(session_id, store)
    run_id = manifest.run_id

    # 幂等：同一运行已发布过的仪表盘直接读回（probe 预算/证据全部保留）；
    # force=True（追问追加新视图后）跳过读回、就地重合成并重新原子发布。
    published_path = store.run_dir(session_id, run_id) / "dashboard.json"
    if published_path.exists() and not force:
        return DashboardArtifact.model_validate(
            store.read_dashboard(session_id, run_id=run_id)
        )

    bundle = AnalysisBundle.model_validate(
        store.read_bundle(session_id, run_id=run_id)
    )
    execution = BundleExecutionResult.model_validate(
        store.read_bundle_execution(session_id)
    )
    if execution.run_id and execution.run_id != run_id:
        raise StaleRunError("执行结果属于其他运行版本，请重新执行当前分析蓝图。")
    dictionary = DataDictionary.model_validate(
        store.read_artifact(session_id, "dictionary")
    )
    snapshot = store.load_snapshot(session_id)
    # T06：固化在 manifest 中的全局筛选口径 → 裁剪范围快照。
    # KPI/Probe/Finding 全部在此同一范围内计算，筛选后可见内容必须一致。
    norm_scope = normalize_scope(snapshot, manifest.scope)
    scope_snapshot = apply_scope_filters(snapshot, norm_scope)

    views = {v.view_id: v for v in bundle.analysis_views}
    # T04：只有 status=success 且结果有效（pass/warn）才可进入仪表盘
    consumable_ids = set(consumable_results(execution).keys())

    # T07：执行后价值评估（数据有效性/影响/证据/新证据/冗余/展示成本）
    assessments = filter_run(bundle, execution)

    runner = ProbeRunner(session_id, store, scope_snapshot)
    kpis = _build_kpis(
        session_id, store, bundle, views, execution, dictionary, runner,
        scope_snapshot,
    )

    # ChartSpec 随 view 级目录落盘；字段必须真实存在于输出 schema（错配拒绝）
    specs: dict[str, ChartSpec] = {}
    for vid in consumable_ids:
        spec = build_chart_spec(views[vid])
        if spec is not None:
            df = _view_result(store, session_id, views[vid])
            validate_chart_fields(spec, df.columns)
            store.write_view_chart_spec(
                session_id, vid, spec.model_dump(mode="json")
            )
            specs[vid] = spec

    sections = _build_sections(bundle, consumable_ids)
    cards = _build_view_cards(
        session_id, store, bundle, views, execution, run_id, specs, assessments,
        dictionary,
    )
    selected_by_col = {
        str(f.get("column", "")): [str(v) for v in f.get("values", [])]
        for f in manifest.scope
    }
    filters = _build_filters(
        bundle, dictionary, snapshot, selected_by_col
    )

    if findings is None:
        # scope_tag：当前全局筛选口径指纹，作为 Finding 去重键的 scope 部分
        scope_tag = hashlib.sha256(
            json.dumps(manifest.scope, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:12]
        findings = detect_findings(
            session_id, store, bundle, execution, dictionary, runner,
            scope_tag=scope_tag,
        )
    risks = [f for f in findings if f.type == "risk"]

    # T07：state 只统计 presentation 视图——computation 证据任务隐藏与否
    # 不影响整体状态；默认展示的核心视图全部可消费才是 ready。
    presentation_cards = [c for c in cards.values() if c.role == "presentation"]
    consumable_count = sum(1 for c in presentation_cards if c.consumable)
    if consumable_count == 0:
        state = "empty"       # 筛选后全部无数据：空态，不是错误，绝不伪造 0
    elif consumable_count == len(presentation_cards):
        state = "ready"
    else:
        state = "partial"

    scope = DashboardScope(
        filters=[
            AppliedFilter(
                column=str(f.get("column", "")),
                values=[str(v) for v in f.get("values", [])],
            )
            for f in manifest.scope
            if str(f.get("column", ""))
        ],
        snapshot_rows=len(snapshot.index),
        participating_rows=len(scope_snapshot.index),
    )
    failed_views = [
        FailedView(view_id=c.view_id, title=c.title, reason=c.reason)
        for c in cards.values()
        if c.role == "presentation" and c.status == ViewStatus.failed
    ]

    # Task 7：深挖链探针卡 + 12-column 布局
    all_cards = dict(cards)
    try:
        chain_set = ChainSet.model_validate(
            store.read_artifact(session_id, "chains")
        )
    except StorageError:
        chain_set = None
    if chain_set is not None:
        all_cards.update(
            _build_probe_cards(session_id, store, run_id, chain_set)
        )

    # 与真实信号/发现关联的视图：它们「有说法、有证据」，排序应靠前
    signal_view_ids: set[str] = set()
    try:
        sig_payload = store.read_artifact(session_id, "signals")
        for sig in sig_payload["signals"]:
            signal_view_ids.update(sig["scan_view_ids"])
    except StorageError:
        pass
    for f in findings:
        signal_view_ids.update(f.evidence_view_ids)

    layout = compose_layout(
        kpis=kpis, cards=all_cards, findings=findings, chain_set=chain_set,
        signal_view_ids=signal_view_ids,
    )

    artifact = DashboardArtifact(
        run_id=run_id,
        bundle_id=bundle.bundle_id,
        title=bundle.title,
        state=state,
        scope=scope,
        kpis=kpis,
        sections=sections,
        views=all_cards,
        findings=findings,
        risks=risks,
        failed_views=failed_views,
        global_filters=filters,
        layout=layout,
    )
    # expected_run_id：迟到的合成响应若发现 active 已前进，绝不回切 current
    store.write_dashboard(
        session_id, artifact.model_dump(mode="json"), expected_run_id=run_id
    )
    return artifact
