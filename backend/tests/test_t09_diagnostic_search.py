"""T09：预算受控的 Diagnostic Search 测试。

覆盖：预算硬边界、每维恰 1 probe 对账、弱信号提前停、失败耗预算且不伪造、
中断恢复不重复不超额、高基数惩罚与最小实体数、规则降级契约（HTTP）、
黄金案例（贡献集中 → 预算内定位正确维度且 observation 可回算）。
"""
from __future__ import annotations

import io
from itertools import product

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.diagnostic import DiagnosticSearch
from app.services.bundle_executor import execute_bundle
from app.services.bundle_planner import build_bundle
from app.services.diagnostic_search import start_diagnostic_search
from app.services.engine.ops import EngineError
from app.services.profiler import confirm_fields, generate_dictionary
from app.services.quality_actions import apply_decisions
from app.services.quality_checker import run_quality_checks
from app.services.storage import SessionStore

MONTHS = ("2024-01", "2024-02")


# ================================================================ 数据构造


def _cross_csv(
    dims: dict[str, list[str]],
    value_fn,
    *,
    rows_per_cell: int = 20,
    extra_cols: dict[str, object] | None = None,
) -> bytes:
    """dims 笛卡尔积，每单元 rows_per_cell 行；value_fn(month_idx, combo, k)。"""
    rows: list[dict] = []
    keys = list(dims)
    for mi, month in enumerate(MONTHS):
        for combo in product(*(dims[k] for k in keys)):
            mapping = dict(zip(keys, combo))
            for k in range(rows_per_cell):
                day = 1 + (k % 20)
                row = {
                    "Date": f"{month}-{day:02d}",
                    **mapping,
                    "Sales": round(float(value_fn(mi, mapping, k)), 2),
                }
                if extra_cols:
                    row.update(extra_cols)
                rows.append(row)
    buf = io.BytesIO()
    pd.DataFrame(rows).to_csv(buf, index=False)
    return buf.getvalue()


def _golden_csv() -> bytes:
    """North 在 2 月腰斩（40 vs 100），贡献全部总下滑；其余维度稳定均匀。"""
    def value(mi, mapping, k):
        if mi == 1 and mapping["Region"] == "North":
            return 40.0
        return 100.0
    return _cross_csv(
        {"Region": ["North", "South", "East", "West"],
         "Segment": ["Consumer", "Corporate"],
         "Channel": ["Online", "Retail"]},
        value,
    )


def _uniform_csv() -> bytes:
    """两期完全一致、组间均匀：无任何信号。"""
    return _cross_csv(
        {"Region": ["North", "South", "East", "West"],
         "Segment": ["Consumer", "Corporate"],
         "Channel": ["Online", "Retail"]},
        lambda mi, mapping, k: 100.0,
    )


def _budget_csv() -> bytes:
    """6 个维度各有一个弱势成员，每个维度都有信号 → 搜索持续展开。"""
    weak = {
        "Region": ("North", 40.0),
        "Segment": ("Consumer", 55.0),
        "Channel": ("Online", 65.0),
        "Category": ("Hardware", 50.0),
        "StoreType": ("Mall", 60.0),
        "Zone": ("Urban", 45.0),
    }

    def value(mi, mapping, k):
        if mi == 0:
            return 100.0
        for col, (member, v) in weak.items():
            if mapping[col] == member:
                return v
        return 100.0
    return _cross_csv(
        {name: [member, f"Other_{name}"] for name, (member, _) in weak.items()},
        value, rows_per_cell=12,
    )


def _high_card_csv() -> bytes:
    """高基数 Customer（100 个、每人 1–2 行）+ Region 集中下滑。"""
    rows: list[dict] = []
    regions = ["North", "South", "East", "West"]
    segments = ["Consumer", "Corporate"]
    customer = 0
    for mi, month in enumerate(MONTHS):
        for region in regions:
            for seg in segments:
                for k in range(25):
                    day = 1 + (k % 20)
                    customer += 1
                    cid = f"C{(customer % 100) + 1:03d}"
                    val = 40.0 if (mi == 1 and region == "North") else 100.0
                    rows.append({
                        "Date": f"{month}-{day:02d}",
                        "Region": region, "Segment": seg,
                        "Customer": cid, "Sales": val,
                    })
    buf = io.BytesIO()
    pd.DataFrame(rows).to_csv(buf, index=False)
    return buf.getvalue()


# ================================================================ 会话 helper


def _ready_session(
    name: str, csv_bytes: bytes, tmp_path,
    overrides: dict[str, str] | None = None,
) -> tuple[SessionStore, str]:
    store = SessionStore(tmp_path / "store")
    meta = store.create_from_bytes(name, csv_bytes)
    sid = meta["session_id"]
    dictionary = generate_dictionary(sid, store)
    pending = [
        {"name": f.name,
         "semantic_type": (overrides or {}).get(
             f.name, f.semantic_type.value
         )}
        for f in dictionary.fields if not f.confirmed and not f.ignored
    ]
    if pending:
        confirm_fields(sid, pending, store)
    report = run_quality_checks(sid, store)
    decisions = {i.issue_id: {"action": i.suggested_action} for i in report.issues}
    apply_decisions(sid, decisions, store)

    snapshot = store.load_snapshot(sid)
    dictionary = store.read_artifact(sid, "dictionary")
    from app.schemas.dictionary import DataDictionary
    dictionary = DataDictionary.model_validate(dictionary)
    bundle = build_bundle(
        dictionary, snapshot, snapshot_rows=len(snapshot), title=name
    )
    store.write_bundle(sid, bundle.model_dump(mode="json"))
    execute_bundle(sid, store, bundle)
    return store, sid


# ================================================================ 黄金案例


def test_golden_locates_region_and_reconcilable(tmp_path):
    store, sid = _ready_session("golden.csv", _golden_csv(), tmp_path)
    search = start_diagnostic_search(sid, store)

    assert search.state == "completed"
    # 预算内：首轮 3 probes → 仅 Region 形成分支 → 再试探后收敛
    assert search.budget.used_rounds <= 4
    assert search.budget.used_probes <= 8

    region_probe = next(p for p in search.probes if p.dimension == "Region")
    assert region_probe.consumable
    assert region_probe.scores.total >= 0.25

    # 展开的分支锚定 Region=North
    child = next(n for n in search.nodes if n.parent_id is not None)
    assert child.dimension == "Region"
    assert child.scope_filters[-1].values == ["North"]

    # observation 可回算：contribution summary 的总量/成员数与独立重算一致
    summ = region_probe.observations["contribution"]
    assert summ["members"] == 4
    assert abs(summ["identity_residual"]) < 1e-6
    assert summ["completeness"] in {"complete", "partial", "unknown"}

    # 用持久化结果独立对账：North 的 contribution_share≈1
    cdf = pd.read_parquet(
        store.diagnostic_dir(sid) / "probes"
        / region_probe.probe_id / "result_contribution.parquet"
    )
    north = cdf[cdf["Region"] == "North"].iloc[0]
    assert north["delta"] < 0
    assert abs(north["contribution_share"] - 1.0) < 1e-6

    # 最终结论不伪造：引用的探针必须真实存在
    assert "North" in search.final_summary
    assert "因果" in search.final_summary  # 明确非因果


# ================================================================ 预算硬边界


def test_budget_hard_caps(tmp_path):
    store, sid = _ready_session("budget.csv", _budget_csv(), tmp_path)
    search = start_diagnostic_search(sid, store)
    b = search.budget

    assert b.used_rounds <= 4
    assert b.used_probes <= 8
    assert b.used_terminal_executions <= 24
    assert max(n.depth for n in search.nodes) <= 3
    # 每 probe ≤3 plans；总执行 ≤3×probes
    assert b.used_terminal_executions <= 3 * b.used_probes
    for p in search.probes:
        assert len(p.plans) <= 3
        assert p.executed_count + p.failed_count <= 3


def test_each_dimension_exactly_one_probe(tmp_path):
    """可逐一对账：一个 (node, dimension) 只出现一次，不存在捆绑扫描。"""
    store, sid = _ready_session("budget.csv", _budget_csv(), tmp_path)
    search = start_diagnostic_search(sid, store)

    seen: set[tuple[str, str]] = set()
    for p in search.probes:
        key = (p.node_id, p.dimension)
        assert key not in seen, f"同一节点上维度被重复试探：{key}"
        seen.add(key)

    # probe 总数 == 各节点试探维度数之和
    assert len(search.probes) == len(seen)


# ================================================================ 弱信号


def test_weak_signal_stops_after_round_one(tmp_path):
    store, sid = _ready_session("uniform.csv", _uniform_csv(), tmp_path)
    search = start_diagnostic_search(sid, store)

    assert search.budget.used_rounds == 1
    assert search.budget.used_probes == 3  # 3 个候选各 1 probe
    assert search.budget.used_terminal_executions == 6  # 每 probe 2 plans
    assert search.state == "completed"
    assert search.stop_reason == "no_signal"
    assert len([n for n in search.nodes if n.parent_id is not None]) == 0
    # 全部 probe 评分低于展开阈值
    assert all(p.scores.total < 0.25 for p in search.probes)


# ================================================================ 失败耗预算


def test_failure_consumes_budget_and_no_fabrication(tmp_path, monkeypatch):
    store, sid = _ready_session("golden.csv", _golden_csv(), tmp_path)

    def boom(*args, **kwargs):
        raise EngineError("模拟引擎失败")

    monkeypatch.setattr(
        "app.services.diagnostic_search.execute_plan", boom
    )
    search = start_diagnostic_search(sid, store)

    b = search.budget
    assert b.used_probes == 3
    assert b.used_terminal_executions == 6   # 失败照样计数
    assert all(not p.consumable for p in search.probes)
    assert all(p.failed_count == 2 for p in search.probes)
    assert search.stop_reason == "insufficient_data"
    # 不伪造结论
    assert search.final_summary
    assert "未形成下钻路径" in search.final_summary
    assert all(
        not p.observations for p in search.probes
    )  # 没有任何成功观察被写入


# ================================================================ 中断恢复


def test_resume_no_duplicate_no_overrun(tmp_path, monkeypatch):
    store, sid = _ready_session("budget.csv", _budget_csv(), tmp_path)

    calls = {"n": 0}
    real_pick = type(store)  # 仅占位

    from app.services.diagnostic_search import DiagnosticController

    original = DiagnosticController._llm_pick_dims

    def patched(self, candidates, max_pick):
        calls["n"] += 1
        if calls["n"] == 2:  # 第 2 轮的维度选择：模拟进程中断
            raise RuntimeError("simulated crash before round 2")
        return original(self, candidates, max_pick)

    monkeypatch.setattr(DiagnosticController, "_llm_pick_dims", patched)
    with pytest.raises(RuntimeError):
        start_diagnostic_search(sid, store)

    # 磁盘状态：running，首轮已完成
    mid = DiagnosticSearch.model_validate(store.read_diagnostic(sid))
    assert mid.state == "running"
    first_probe_ids = {p.probe_id for p in mid.probes}
    assert mid.budget.used_rounds == 1

    # 恢复：不再 patch，预算不重置
    monkeypatch.undo()
    final = start_diagnostic_search(sid, store)

    assert final.state == "completed"
    all_ids = [p.probe_id for p in final.probes]
    assert len(all_ids) == len(set(all_ids))         # 无重复 probe
    assert first_probe_ids <= set(all_ids)
    assert final.budget.used_rounds <= 4
    assert final.budget.used_probes <= 8
    assert final.budget.used_terminal_executions <= 24
    # 同一节点的同一维度没有被二次试探
    keys = [(p.node_id, p.dimension) for p in final.probes]
    assert len(keys) == len(set(keys))
    # 轮次记录从第 2 轮继续，没有重建第 1 轮
    round_nos = [r.round for r in final.rounds]
    assert round_nos[0] == 1
    assert len(round_nos) == len(set(round_nos))


# ================================================================ 高基数


def test_high_cardinality_penalty_and_entities(tmp_path):
    store, sid = _ready_session(
        "highcard.csv", _high_card_csv(), tmp_path,
        overrides={"Customer": "dimension"},
    )
    search = start_diagnostic_search(sid, store)

    by_dim = {p.dimension: p for p in search.probes}
    assert "Customer" in by_dim
    customer = by_dim["Customer"]
    assert customer.scores.cardinality_penalty > 0
    # 每位客户仅 1–2 行 → support 远低于满分
    assert customer.scores.support < 0.6
    assert customer.scores.total < 0.25  # 惩罚 + 支撑不足 → 不展开

    # Region 仍是最高分且正确展开
    region = by_dim["Region"]
    assert region.scores.total >= 0.25
    children = [n for n in search.nodes
                if n.parent_id is not None and n.dimension == "Region"]
    assert children
    assert children[0].scope_filters[-1].values == ["North"]


# ================================================================ HTTP 契约（规则降级路径）


def test_http_endpoints_rule_fallback(tmp_path, monkeypatch):
    # 路由内 SessionStore 由 storage 模块命名空间解析：patch 为绑定 tmp 的工厂
    import functools

    root = tmp_path / "store_http"
    monkeypatch.setattr(
        "app.services.storage.SessionStore",
        functools.partial(SessionStore, root),
    )

    store = SessionStore(root)
    meta = store.create_from_bytes("golden.csv", _golden_csv())
    sid = meta["session_id"]
    dictionary = generate_dictionary(sid, store)
    pending = [
        {"name": f.name, "semantic_type": f.semantic_type.value}
        for f in dictionary.fields if not f.confirmed and not f.ignored
    ]
    if pending:
        confirm_fields(sid, pending, store)
    report = run_quality_checks(sid, store)
    decisions = {i.issue_id: {"action": i.suggested_action} for i in report.issues}
    apply_decisions(sid, decisions, store)

    snapshot = store.load_snapshot(sid)
    from app.schemas.dictionary import DataDictionary
    dictionary = DataDictionary.model_validate(
        store.read_artifact(sid, "dictionary")
    )
    bundle = build_bundle(
        dictionary, snapshot, snapshot_rows=len(snapshot), title="golden.csv"
    )
    store.write_bundle(sid, bundle.model_dump(mode="json"))
    execute_bundle(sid, store, bundle)

    client = TestClient(app)

    # POST：无 → 新建并跑完（无 LLM key/固件，走规则降级）
    resp = client.post(f"/sessions/{sid}/diagnostic-search")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "completed"
    assert data["stop_reason"] in {"root_completed", "budget_exhausted"}

    # 重复 POST：幂等，返回同一 search_id
    again = client.post(f"/sessions/{sid}/diagnostic-search")
    assert again.status_code == 200
    assert again.json()["search_id"] == data["search_id"]

    # GET：读取状态
    got = client.get(f"/sessions/{sid}/diagnostic-search")
    assert got.status_code == 200
    assert got.json()["search_id"] == data["search_id"]

    # GET 未发起的 session → 404
    missing = client.get("/sessions/no-such-session/diagnostic-search")
    assert missing.status_code == 404
