"""Task 5：Evidence Graph 证据图谱测试。

覆盖：
- TR-5.1 全部 signal/chain/view/probe 引用在图中可解析，无悬空；
- TR-5.2 反向查询与正向边闭合一致；chain_for 正确；
        GET /evidence 全图与 ?node= 子图。
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.schemas.auto import EvidenceGraph
from app.services.evidence_graph import (
    build_evidence_graph,
    chain_for,
    downstream,
    upstream,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_chain_diagnostic import _chains_with_signals  # noqa: E402
from test_signals import _superstore_df  # noqa: E402


def _graph(tmp_path: Path) -> tuple:
    store, sid, run_id, bundle, chain_set = _chains_with_signals(
        tmp_path, _superstore_df()
    )
    graph = build_evidence_graph(sid, store)
    return store, sid, graph, chain_set


# ------------------------------------------------------------ TR-5.1


def test_every_reference_resolves(tmp_path):
    store, sid, graph, chain_set = _graph(tmp_path)
    ids = {n.node_id for n in graph.nodes}

    for e in graph.edges:
        assert e.source in ids and e.target in ids

    # chain 中每个节点引用都必须在图中
    for c in chain_set.chains:
        assert c.chain_id in ids
        for n in c.nodes:
            assert n.ref_id in ids


def test_graph_has_expected_node_types(tmp_path):
    store, sid, graph, chain_set = _graph(tmp_path)
    types = {n.type for n in graph.nodes}

    assert {"view", "signal"} <= types
    if chain_set.chains:
        assert "chain" in types


# ------------------------------------------------------------ TR-5.2


def test_upstream_downstream_close_over_edges(tmp_path):
    store, sid, graph, chain_set = _graph(tmp_path)

    for e in graph.edges:
        assert e.target in downstream(graph, e.source)
        assert e.source in upstream(graph, e.target)

    # signal → chain 的 root 边
    for c in chain_set.chains:
        assert chain_for(graph, c.root_signal_id) == c.chain_id


def test_evidence_endpoint_full_and_neighborhood(tmp_path, monkeypatch):
    import dataclasses

    from fastapi.testclient import TestClient

    import app.services.storage as storage_mod
    from app.config import settings as app_settings
    from app.main import app

    # 让路由内 SessionStore() 指向本测试的临时目录
    test_settings = dataclasses.replace(
        app_settings, storage_dir=tmp_path / "store"
    )
    monkeypatch.setattr(storage_mod, "settings", test_settings)
    store, sid, graph, chain_set = _graph(tmp_path)
    client = TestClient(app)

    resp = client.get(f"/sessions/{sid}/evidence")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["nodes"]) == len(graph.nodes)

    probe = next(
        (e.target for e in graph.edges if e.type == "uses"), None
    )
    assert probe is not None
    sub = client.get(f"/sessions/{sid}/evidence", params={"node": probe})
    assert sub.status_code == 200
    sub_ids = {n["node_id"] for n in sub.json()["nodes"]}
    assert probe in sub_ids

    missing = client.get(
        f"/sessions/{sid}/evidence", params={"node": "nope_xyz"}
    )
    assert missing.status_code == 404
