"""证据图谱（Task 5）。

构建类型化证据索引：
    scan view/probe ──evidence──▶ signal ──root──▶ chain
    chain ──uses──▶ view/probe
（Task 6 追加 claim 节点与 supports 边。）

查询：upstream/downstream、chain_for、claims_using；构建时解析校验，
任何悬空引用直接抛错。
"""
from __future__ import annotations

from collections import deque

from app.schemas.auto import (
    EvidenceEdge,
    EvidenceGraph,
    EvidenceNode,
    SignalSet,
)
from app.schemas.bundle import AnalysisBundle
from app.services.diagnostic_search import _now
from app.services.storage import SessionStore, StorageError


# ------------------------------------------------------------ 构建


def build_evidence_graph(
    session_id: str, store: SessionStore
) -> EvidenceGraph:
    signal_set = SignalSet.model_validate(
        store.read_artifact(session_id, "signals")
    )
    run_id = signal_set.run_id
    bundle = AnalysisBundle.model_validate(
        store.read_bundle(session_id, run_id)
    )

    nodes: dict[str, EvidenceNode] = {
        v.view_id: EvidenceNode(
            node_id=v.view_id, type="view",
            label=f"{getattr(v.type, 'value', v.type)}·"
                  f"{v.metric_fields[0] if v.metric_fields else ''}",
        )
        for v in bundle.analysis_views
    }
    edges: list[EvidenceEdge] = []

    # signals
    for s in signal_set.signals:
        nodes[s.signal_id] = EvidenceNode(
            node_id=s.signal_id, type="signal",
            label=f"{s.type}·{s.metric}",
        )
        for vid in s.scan_view_ids:
            edges.append(EvidenceEdge(
                source=vid, target=s.signal_id, type="evidence"
            ))

    # chains（diagnostic 跳过/未产出时缺席）
    try:
        chain_payload = store.read_artifact(session_id, "chains")
    except StorageError:
        chain_payload = None
    if chain_payload:
        for c in chain_payload["chains"]:
            cid = c["chain_id"]
            nodes[cid] = EvidenceNode(
                node_id=cid, type="chain", label=c["root_question"]
            )
            edges.append(EvidenceEdge(
                source=c["root_signal_id"], target=cid, type="root"
            ))
            for n in c["nodes"]:
                ref = n["ref_id"]
                if ref not in nodes:
                    # 未在视图集合中的证据 = probe
                    nodes[ref] = EvidenceNode(
                        node_id=ref, type="probe",
                        label=f"{n.get('dimension') or ''}·{n['kind']}",
                    )
                edges.append(EvidenceEdge(
                    source=cid, target=ref, type="uses"
                ))

    # 解析校验：边端点必须存在
    ids = set(nodes)
    for e in edges:
        if e.source not in ids:
            raise StorageError(f"证据图悬空边起点：{e.source}")
        if e.target not in ids:
            raise StorageError(f"证据图悬空边终点：{e.target}")

    graph = EvidenceGraph(
        session_id=session_id,
        run_id=run_id,
        nodes=list(nodes.values()),
        edges=edges,
        created_at=_now(),
    )
    store.write_artifact(
        session_id, "evidence", graph.model_dump(mode="json")
    )
    return graph


# ------------------------------------------------------------ 查询


def _adj(graph: EvidenceGraph) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    forward: dict[str, list[str]] = {}
    backward: dict[str, list[str]] = {}
    for e in graph.edges:
        forward.setdefault(e.source, []).append(e.target)
        backward.setdefault(e.target, []).append(e.source)
    return forward, backward


def _bfs(start: str, adj: dict[str, list[str]]) -> list[str]:
    seen = {start}
    queue: deque[str] = deque([start])
    while queue:
        cur = queue.popleft()
        for nxt in adj.get(cur, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    seen.discard(start)
    return sorted(seen)


def downstream(graph: EvidenceGraph, node_id: str) -> list[str]:
    forward, _ = _adj(graph)
    return _bfs(node_id, forward)


def upstream(graph: EvidenceGraph, node_id: str) -> list[str]:
    _, backward = _adj(graph)
    return _bfs(node_id, backward)


def chain_for(graph: EvidenceGraph, signal_id: str) -> str | None:
    targets = [
        e.target for e in graph.edges
        if e.source == signal_id and e.type == "root"
    ]
    return targets[0] if targets else None


def claims_using(graph: EvidenceGraph, view_id: str) -> list[str]:
    """引用该 view/probe 的 claim（Task 6 接入 supports 边）。"""
    return [
        e.source for e in graph.edges
        if e.target == view_id and e.type == "supports"
    ]


def neighborhood(graph: EvidenceGraph, node_id: str) -> EvidenceGraph:
    """节点的上下游闭包子图（供 ?node= 过滤）。"""
    reach = {node_id}
    reach.update(upstream(graph, node_id))
    reach.update(downstream(graph, node_id))
    return EvidenceGraph(
        session_id=graph.session_id,
        run_id=graph.run_id,
        nodes=[n for n in graph.nodes if n.node_id in reach],
        edges=[
            e for e in graph.edges
            if e.source in reach and e.target in reach
        ],
        created_at=graph.created_at,
    )
