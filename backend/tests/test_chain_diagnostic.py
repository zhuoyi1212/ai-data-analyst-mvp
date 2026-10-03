"""Task 4：Multi-root Diagnostic 多信号深挖测试。

覆盖验收：
- TR-4.1 Superstore 类数据至少 1 条 chain 深度≥2，第二层为新的下钻/交叉视角；
- TR-4.2 每个节点引用的 view/probe 真实存在、可回算，无悬空引用；
- TR-4.3 chains/probes/终端执行/depth 预算不可突破，耗尽时 stop_reason 明确；
        空信号集合不产出 chain。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from app.schemas.auto import ChainNode, SignalSet
from app.services.chain_diagnostic import (
    GLOBAL_MAX_EXEC,
    GLOBAL_MAX_PROBES,
    MAX_CHAINS,
    MAX_DEPTH,
    PER_CHAIN_MAX_PROBES,
    run_chain_diagnostic,
)
from app.services.signals import build_signal_set

sys.path.insert(0, str(Path(__file__).parent))
from test_signals import _superstore_df, _run_scan_and_signals  # noqa: E402


def _chains_with_signals(tmp_path: Path, df: pd.DataFrame):
    store, sid, bundle, execution, signals = _run_scan_and_signals(tmp_path, df)
    run_id = store.active_run_id(sid)
    ss = build_signal_set(sid, run_id, signals)
    store.write_artifact(sid, "signals", ss.model_dump(mode="json"))
    chain_set = run_chain_diagnostic(sid, store)
    return store, sid, run_id, bundle, chain_set


# ------------------------------------------------------------ TR-4.1


def test_chain_reaches_depth_two_with_new_views(tmp_path):
    store, sid, run_id, bundle, chain_set = _chains_with_signals(
        tmp_path, _superstore_df()
    )

    deep = [
        n for c in chain_set.chains for n in c.nodes if n.depth >= 2
    ]
    assert deep, "至少一条 chain 深度应 ≥ 2"

    # 第二层节点：scope 链严格长于父节点，且维度/范围不同源
    for n in deep:
        parent = next(
            p for c in chain_set.chains for p in c.nodes
            if p.node_id == n.parent_id
        )
        assert len(n.scope_filters) > len(parent.scope_filters)
        assert n.ref_type == "probe" and n.ref_id != parent.ref_id


# ------------------------------------------------------------ TR-4.2


def test_all_node_refs_resolve(tmp_path):
    store, sid, run_id, bundle, chain_set = _chains_with_signals(
        tmp_path, _superstore_df()
    )
    valid_views = {v.view_id for v in bundle.analysis_views}
    probes_root = store.run_dir(sid, run_id) / "diagnostic" / "probes"

    for c in chain_set.chains:
        for n in c.nodes:
            if n.ref_type == "view":
                assert n.ref_id in valid_views
                assert (
                    store.view_dir(sid, n.ref_id) / "result.parquet"
                ).exists()
            else:
                pdir = probes_root / n.ref_id
                assert pdir.exists(), f"悬空 probe 引用：{n.ref_id}"
                # 至少一个终端计划成功并留下可回算结果
                results = list(pdir.glob("result_*.parquet"))
                assert results
                for r in results:
                    out = pd.read_parquet(r)
                    assert len(out.index) > 0


# ------------------------------------------------------------ TR-4.3


def test_budget_hard_limits_and_stop_reasons(tmp_path):
    store, sid, run_id, bundle, chain_set = _chains_with_signals(
        tmp_path, _superstore_df()
    )

    assert len(chain_set.chains) <= MAX_CHAINS
    assert chain_set.budget["used_probes"] <= GLOBAL_MAX_PROBES
    assert chain_set.budget["used_terminal_executions"] <= GLOBAL_MAX_EXEC

    for c in chain_set.chains:
        assert c.depth <= MAX_DEPTH
        assert c.budget_used["probes"] <= PER_CHAIN_MAX_PROBES
        assert c.stop_reason  # 预算耗尽/收敛原因必须明确
        for n in c.nodes:
            assert n.depth <= MAX_DEPTH


def test_empty_signal_set_produces_no_chains(tmp_path):
    store, sid, bundle, execution, signals = _run_scan_and_signals(
        tmp_path,
        pd.DataFrame({
            "category": ["A", "B", "C"] * 20,
            "sales": [100.0] * 60,
            "profit": [20.0] * 60,
        }),
    )
    run_id = store.active_run_id(sid)
    ss = build_signal_set(sid, run_id, signals)
    store.write_artifact(sid, "signals", ss.model_dump(mode="json"))

    chain_set = run_chain_diagnostic(sid, store)

    assert chain_set.chains == []
    assert chain_set.budget["used_probes"] == 0
    assert chain_set.budget["used_terminal_executions"] == 0
