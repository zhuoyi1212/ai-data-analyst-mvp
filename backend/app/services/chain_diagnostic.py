"""多 root 自动深挖（Task 4）。

以 T09 `DiagnosticController` 为内核：为 SignalSet 的每个 top 信号各起一条
AnalysisChain，root 问题由信号自动生成；多 root 顺序执行、共享全局预算。

预算硬边界（失败也计数、不重置；chains.json 是唯一事实）：
    chains ≤ 3，总 probes ≤ 12，总终端执行 ≤ 36，depth ≤ 3；
    每条 chain 局部 ≤ 4 probes / ≤ 12 终端。

红线：数值仍只来自确定性算子；LLM 只在 T09 内核中选试探维度/写结论；
相关类 root 问题带“相关不等于因果”护栏。
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from app.schemas.auto import (
    AnalysisChain,
    ChainNode,
    ChainSet,
    DiagnosticViewRef,
    Signal,
    SignalSet,
)
from app.schemas.bundle import AnalysisBundle
from app.schemas.diagnostic import (
    DiagnosticSearch,
    ScopeFilter,
    SearchBudget,
    SearchNode,
)
from app.schemas.dictionary import DataDictionary
from app.services.bundle_planner import _spec_func
from app.services.diagnostic_search import ROOT_ID, DiagnosticController, _now
from app.services.metric_spec import effective_metric_spec
from app.services.storage import SessionStore

# ---------------------------------------------------------------- 预算

MAX_CHAINS = 3
GLOBAL_MAX_PROBES = 12
GLOBAL_MAX_EXEC = 36
PER_CHAIN_MAX_PROBES = 4
PER_CHAIN_MAX_EXEC = 12
MAX_DEPTH = 3

_LABELS: dict[str, str] = {
    "group_by": "分组对比",
    "contribution": "变化贡献",
    "rate_decomposition": "率结构分解",
}


# ------------------------------------------------------------ root 构造


def _controller_metric(signal: Signal, dictionary: DataDictionary) -> str | None:
    """复合指标（x~y / x/y）中选出一个真实可计算列。

    相关 x~y 取后者（如 discount~profit → profit）；率 x/y 取分子。
    """
    metric = signal.metric
    if "~" in metric:
        picks = metric.split("~")[::-1][:1]
    elif "/" in metric:
        picks = metric.split("/")[:1]
    else:
        picks = [metric]
    cols = {f.name for f in dictionary.fields if not f.ignored}
    return next((p for p in picks if p in cols), None)


def _root_question(signal: Signal) -> str:
    metric = signal.metric
    if signal.type == "decline":
        change = signal.magnitude.change_pct
        tail = f"（环比 {abs(change):.1f}%）" if change is not None else ""
        return f"为什么「{metric}」环比下滑{tail}？"
    if signal.type == "growth":
        return f"「{metric}」增长的驱动来自哪里？"
    if signal.type in ("negative_member", "divergence"):
        return (
            f"为什么「{signal.dimension}={signal.member}」的"
            f"「{metric}」为负？"
        )
    if signal.type == "scale_profit":
        return "增收不增利主要由哪些维度造成？"
    if signal.type == "simpson":
        return "为什么整体率下降、各组内部率反而改善？"
    if signal.type == "correlation":
        return (
            f"「{metric}」的显著相关在哪些范围内更明显？"
            "（相关不等于因果，仅作线索）"
        )
    if signal.type == "anomaly":
        return f"「{metric}」的极端值集中在哪些范围？"
    return f"「{metric}」的表现由哪些维度驱动？"


def _root_scope(
    signal: Signal, snapshot: pd.DataFrame
) -> tuple[list[ScopeFilter], pd.DataFrame]:
    """成员类信号：root 即限定在该成员内，向其他维度下钻。"""
    if signal.type not in ("negative_member", "divergence"):
        return [], snapshot
    dim, member = signal.dimension, signal.member
    if not dim or not member or dim not in snapshot.columns:
        return [], snapshot
    present = {str(v): v for v in snapshot[dim].dropna().unique()}
    if member not in present:
        return [], snapshot
    scopes = [ScopeFilter(column=dim, values=[member])]
    return scopes, snapshot[snapshot[dim].isin([present[member]])]


# ------------------------------------------------------------ 控制器


class _SilentController(DiagnosticController):
    """深挖链统一落 chains.json；避免覆盖旧 T09 diagnostic.json。"""

    def _persist(self) -> None:  # noqa: D102
        return None


def _run_one(
    idx: int,
    signal: Signal,
    *,
    session_id: str,
    store: SessionStore,
    dictionary: DataDictionary,
    snapshot: pd.DataFrame,
    run_id: str,
    probes_cap: int,
    exec_cap: int,
) -> tuple[AnalysisChain, int, int]:
    root_cn = ChainNode(
        node_id="root",
        kind="root",
        ref_type="view",
        ref_id=signal.scan_view_ids[0],
        dimension=signal.dimension,
        member=signal.member,
        depth=0,
        status="completed",
    )

    metric = _controller_metric(signal, dictionary)
    if metric is None:
        chain = AnalysisChain(
            chain_id=f"chain_{idx:02d}",
            root_signal_id=signal.signal_id,
            root_question=_root_question(signal),
            nodes=[root_cn],
            conclusion="信号指标无法对应到可计算字段，未展开深挖。",
            depth=0,
            status="stopped",
            stop_reason="no_metric",
            budget_used={"probes": 0, "terminal_executions": 0, "rounds": 0},
        )
        return chain, 0, 0

    date_col = dictionary.date_fields()[0] if dictionary.date_fields() else None
    field = next((f for f in dictionary.fields if f.name == metric), None)
    func = _spec_func(effective_metric_spec(field)) if field else "sum"
    func = getattr(func, "value", func) or "sum"

    scopes, scope_df = _root_scope(signal, snapshot)
    bundle = AnalysisBundle.model_validate(
        store.read_bundle(session_id, run_id)
    )
    search = DiagnosticSearch(
        search_id=f"chain_{idx:02d}_{signal.signal_id}",
        run_id=run_id,
        root_question=_root_question(signal),
        root_scope=scopes,
        budget=SearchBudget(
            max_rounds=4,
            max_probes=probes_cap,
            max_depth=MAX_DEPTH,
            max_plans_per_probe=3,
            max_terminal_executions=exec_cap,
        ),
        nodes=[SearchNode(
            node_id=ROOT_ID, depth=0, scope_filters=scopes
        )],
        state="running",
        created_at=_now(),
        updated_at=_now(),
    )
    controller = _SilentController(
        session_id, store, search,
        dictionary=dictionary, scope_snapshot=scope_df,
        metric=metric, func=func, date_col=date_col, bundle=bundle,
    )
    completed = controller.run()
    chain = _convert(completed, signal, idx)
    used = completed.budget
    return chain, used.used_probes, used.used_terminal_executions


# ------------------------------------------------------------ 转换


def _convert(
    search: DiagnosticSearch, signal: Signal, idx: int
) -> AnalysisChain:
    nodes: list[ChainNode] = [ChainNode(
        node_id="root",
        kind="root",
        ref_type="view",
        ref_id=signal.scan_view_ids[0],
        dimension=signal.dimension,
        member=signal.member,
        depth=0,
        status="completed",
    )]

    # T09 树节点 → 展开它的 probe（chain 节点）及其树深度
    node_depth = {n.node_id: n.depth for n in search.nodes}
    node_map: dict[str, str] = {ROOT_ID: "root"}
    for n in search.nodes:
        if n.parent_id is None or not n.scope_filters:
            continue
        last_col = n.scope_filters[-1].column
        probe = next(
            (p for p in search.probes
             if p.node_id == n.parent_id and p.dimension == last_col),
            None,
        )
        if probe is not None:
            node_map[n.node_id] = probe.probe_id

    diag_views: list[DiagnosticViewRef] = []
    for p in search.probes:
        executed_ops = [
            b.op for b in p.plans if b.status == "executed"
        ]
        kind = (
            "driver" if "rate_decomposition" in executed_ops
            else "cross" if "contribution" in executed_ops
            else "drill"
        )
        # 若该 probe 展开了子节点，取出锁定的成员
        child = next(
            (n for n in search.nodes
             if n.parent_id == p.node_id and n.scope_filters
             and n.scope_filters[-1].column == p.dimension),
            None,
        )
        member = (
            child.scope_filters[-1].values[0] if child is not None else None
        )
        nodes.append(ChainNode(
            node_id=p.probe_id,
            parent_id=node_map.get(p.node_id, "root"),
            kind=kind,
            ref_type="probe",
            ref_id=p.probe_id,
            dimension=p.dimension,
            member=member,
            scope_filters=[
                s.model_dump(mode="json") for s in p.scope_filters
            ],
            depth=node_depth.get(p.node_id, 0) + 1,
            scores=(
                p.scores.model_dump(mode="json")
                if p.scores is not None else None
            ),
            status="completed" if p.consumable else "stopped",
        ))
        if p.consumable:
            for op in executed_ops:
                diag_views.append(DiagnosticViewRef(
                    probe_id=p.probe_id,
                    op=op,
                    dimension=p.dimension,
                    metric=signal.metric,
                    label=f"{p.dimension} · {_LABELS.get(op, op)}",
                ))

    depth = max((n.depth for n in nodes), default=0)
    any_evidence = any(
        n.kind != "root" and n.status == "completed" for n in nodes
    )
    budget = search.budget
    return AnalysisChain(
        chain_id=f"chain_{idx:02d}",
        root_signal_id=signal.signal_id,
        root_question=search.root_question,
        nodes=nodes,
        diagnostic_views=diag_views,
        conclusion=search.final_summary or search.root_question,
        depth=depth,
        status="completed" if any_evidence else "stopped",
        stop_reason=search.stop_reason or "no_signal",
        budget_used={
            "probes": budget.used_probes,
            "terminal_executions": budget.used_terminal_executions,
            "rounds": budget.used_rounds,
        },
    )


# ------------------------------------------------------------ 入口


def run_chain_diagnostic(
    session_id: str, store: SessionStore
) -> ChainSet:
    """为 SignalSet 的 top 信号执行多 root 深挖，并落盘 chains 产物。"""
    signal_set = SignalSet.model_validate(
        store.read_artifact(session_id, "signals")
    )
    dictionary = DataDictionary.model_validate(
        store.read_artifact(session_id, "dictionary")
    )
    snapshot = store.load_snapshot(session_id)

    by_id = {s.signal_id: s for s in signal_set.signals}
    roots: list[Signal] = [
        by_id[i] for i in signal_set.top_signal_ids if i in by_id
    ][:MAX_CHAINS]

    remaining_p, remaining_e = GLOBAL_MAX_PROBES, GLOBAL_MAX_EXEC
    chains: list[AnalysisChain] = []
    for idx, signal in enumerate(roots, start=1):
        if remaining_p <= 0 or remaining_e <= 0:
            break
        chain, used_p, used_e = _run_one(
            idx, signal,
            session_id=session_id, store=store,
            dictionary=dictionary, snapshot=snapshot,
            run_id=signal_set.run_id,
            probes_cap=min(PER_CHAIN_MAX_PROBES, remaining_p),
            exec_cap=min(PER_CHAIN_MAX_EXEC, remaining_e),
        )
        remaining_p -= used_p
        remaining_e -= used_e
        chains.append(chain)

    chain_set = ChainSet(
        session_id=session_id,
        run_id=signal_set.run_id,
        chains=chains,
        budget={
            "max_chains": MAX_CHAINS,
            "max_probes": GLOBAL_MAX_PROBES,
            "max_terminal_executions": GLOBAL_MAX_EXEC,
            "used_probes": GLOBAL_MAX_PROBES - remaining_p,
            "used_terminal_executions": GLOBAL_MAX_EXEC - remaining_e,
        },
        created_at=_now(),
    )
    store.write_artifact(
        session_id, "chains", chain_set.model_dump(mode="json")
    )
    return chain_set
