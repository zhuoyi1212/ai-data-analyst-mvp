"""Task 8：追问/下钻后端测试（产物追加不覆盖）。

覆盖：
- TR-8.1 原视图数量不减、result.parquet 字节不变；新 view_id 唯一且可消费；
- TR-8.2 追问在当前 scope 内计算；答案未接地必被拦截；POST /ask 端点。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pandas as pd

from app.schemas.ask import AskSet
from app.services.ask_service import AskError, _answer_claims, ask
from app.services.bundle_planner import apply_scope_filters, normalize_scope

sys.path.insert(0, str(Path(__file__).parent))
from test_report import _report_pipeline  # noqa: E402


def _view_hashes(store, sid, run_id):
    vdir = store.run_dir(sid, run_id) / "views"
    return {
        p.parent.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in vdir.rglob("result.parquet")
    }


# ------------------------------------------------------------ TR-8.1


def test_ask_appends_without_overwriting(tmp_path):
    store, sid, run_id, graph, report = _report_pipeline(tmp_path)
    before = _view_hashes(store, sid, run_id)

    artifact = ask(sid, store, "不同 region 之间 profit 如何对比？")
    after = _view_hashes(store, sid, run_id)

    # 原视图全部保留且字节不变
    assert set(before) < set(after)
    assert len(after) == len(before) + 1
    for vid, digest in before.items():
        assert after[vid] == digest

    # 新视图唯一且可消费
    new_id = artifact.new_view_ids[0]
    assert new_id not in before
    bundle = store.read_bundle(sid, run_id)
    ids = [v["view_id"] for v in bundle["analysis_views"]]
    assert ids.count(new_id) == 1

    result = pd.read_parquet(
        store.run_dir(sid, run_id) / "views" / new_id / "result.parquet"
    )
    assert len(result) >= 1

    # 再追问一次：累积追加
    second = ask(sid, store, "各 segment 的 sales 占比如何？")
    assert second.new_view_ids[0] != new_id
    assert len(_view_hashes(store, sid, run_id)) == len(before) + 2

    asks = AskSet.model_validate(store.read_artifact(sid, "asks"))
    assert len(asks.asks) == 2


# ------------------------------------------------------------ TR-8.2


def test_ask_computed_within_scope(tmp_path):
    store, sid, run_id, graph, report = _report_pipeline(tmp_path)
    manifest = store.run_manifest(sid)
    snapshot = store.load_snapshot(sid)
    scope_df = apply_scope_filters(
        snapshot, normalize_scope(snapshot, manifest.scope)
    )

    artifact = ask(sid, store, "不同 region 之间 profit 如何对比？")
    assert artifact.scope_rows == len(scope_df)

    # 答案数字与新视图真实结果一致
    result = pd.read_parquet(
        store.run_dir(sid, run_id) / "views"
        / artifact.new_view_ids[0] / "result.parquet"
    )
    assert "Central" in set(result["region"])


def test_ungrounded_answer_blocked(tmp_path, monkeypatch):
    store, sid, run_id, graph, report = _report_pipeline(tmp_path)
    # 接地列只含 1.0：真实值 42 无法匹配 → 答案必拒
    monkeypatch.setattr(
        "app.services.ask_service.read_value_columns",
        lambda *a, **k: {"value": (1.0,)},
    )
    df = pd.DataFrame({"dim": ["A"], "value": [42.0]})
    try:
        _answer_claims(
            df, "view_99", store=store, session_id=sid, run_id=run_id
        )
    except AskError as e:
        assert "未接地" in str(e)
    else:
        raise AssertionError("未接地答案应被拦截")


def test_ask_endpoint(tmp_path, monkeypatch):
    import dataclasses

    from fastapi.testclient import TestClient

    from app.config import settings as app_settings
    import app.services.storage as storage_mod
    from app.main import app

    monkeypatch.setattr(
        storage_mod, "settings",
        dataclasses.replace(app_settings, storage_dir=tmp_path / "store"),
    )
    store, sid, run_id, graph, report = _report_pipeline(tmp_path)
    client = TestClient(app)

    resp = client.post(
        f"/sessions/{sid}/ask",
        json={"question": "不同 region 之间 profit 如何对比？"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["new_view_ids"]

    # 空问题被契约拒绝
    bad = client.post(f"/sessions/{sid}/ask", json={"question": ""})
    assert bad.status_code == 422


def test_dashboard_still_available_after_ask(tmp_path, monkeypatch):
    """P0-1 回归：追问后 GET dashboard 必须 200 且含新视图（指针不悬空）。"""
    import dataclasses

    from fastapi.testclient import TestClient

    from app.config import settings as app_settings
    import app.services.storage as storage_mod
    from app.main import app

    monkeypatch.setattr(
        storage_mod, "settings",
        dataclasses.replace(app_settings, storage_dir=tmp_path / "store"),
    )
    store, sid, run_id, graph, report = _report_pipeline(tmp_path)
    client = TestClient(app)

    resp = client.post(
        f"/sessions/{sid}/ask",
        json={"question": "不同 region 之间 profit 如何对比？"},
    )
    assert resp.status_code == 200, resp.text
    new_view = resp.json()["new_view_ids"][0]

    got = client.get(f"/sessions/{sid}/dashboard")
    assert got.status_code == 200, got.text
    assert new_view in got.json()["views"]


def test_ask_rejects_when_snapshot_changed(tmp_path):
    """P1-1 回归：snapshot 字节变化后追问必须拒绝（不旁路指纹门禁）。"""
    store, sid, run_id, graph, report = _report_pipeline(tmp_path)
    original = store.load_snapshot(sid)
    tampered = pd.concat([original, original.iloc[[0]]], ignore_index=True)
    tampered.to_parquet(store.session_dir(sid) / "snapshot.parquet")

    try:
        ask(sid, store, "不同 region 之间 profit 如何对比？")
    except AskError as e:
        assert "快照" in str(e)
    else:
        raise AssertionError("快照变更后追问应被拒绝")


def test_concurrent_asks_serialized(tmp_path):
    """P1-2 回归：并发追问经会话锁串行化，产物不撞号、文件不撕裂。"""
    import threading

    store, sid, run_id, graph, report = _report_pipeline(tmp_path)
    n_before = len(store.read_bundle(sid, run_id)["analysis_views"])
    results: list = []
    errors: list[Exception] = []

    def _run(q: str) -> None:
        try:
            results.append(ask(sid, store, q))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [
        threading.Thread(
            target=_run, args=("不同 region 之间 profit 如何对比？",)
        ),
        threading.Thread(
            target=_run, args=("各 segment 的 sales 表现如何？",)
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(results) == 2
    new_ids = [r.new_view_ids[0] for r in results]
    assert new_ids[0] != new_ids[1]

    bundle = store.read_bundle(sid, run_id)
    assert len(bundle["analysis_views"]) == n_before + 2

    # 两个新视图 parquet 均可正常反序列化
    for vid in new_ids:
        df = pd.read_parquet(
            store.run_dir(sid, run_id) / "views" / vid / "result.parquet"
        )
        assert len(df) >= 1
