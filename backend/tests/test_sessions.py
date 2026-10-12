"""项目记录入口：列表排序 / 状态推导 + 删除（后端）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.storage import SessionStore, StorageError


def _store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "storage")


def _mk_session(store: SessionStore, name: str) -> dict:
    return store.create_from_bytes(name, b"a,b\n1,2\n3,4\n")


def test_delete_session_removes_directory(tmp_path):
    store = _store(tmp_path)
    sid = _mk_session(store, "a.csv")["session_id"]

    store.delete_session(sid)
    with pytest.raises(StorageError):
        store.get_meta(sid)


def test_delete_missing_session_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(StorageError):
        store.delete_session("does-not-exist")


def test_delete_api_and_404(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr("app.routers.sessions._store", lambda: store)
    client = TestClient(app)
    sid = _mk_session(store, "a.csv")["session_id"]

    assert client.delete(f"/sessions/{sid}").status_code == 200
    assert client.delete(f"/sessions/{sid}").status_code == 404


def test_list_sessions_status_and_order(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr("app.routers.sessions._store", lambda: store)
    client = TestClient(app)

    a = _mk_session(store, "a.csv")
    b = _mk_session(store, "b.csv")
    # 控制 created_at 使 b 更新（应排在前），并给 a 写 completed 状态
    store.update_meta(a["session_id"], created_at="2026-01-01T00:00:00+00:00")
    store.update_meta(b["session_id"], created_at="2026-01-02T00:00:00+00:00")
    store.write_artifact(a["session_id"], "auto", {"status": "completed"})

    resp = client.get("/sessions")
    assert resp.status_code == 200
    sessions = resp.json()["sessions"]

    # 按 created_at 倒序：b 在前
    assert [s["session_id"] for s in sessions] == [
        b["session_id"], a["session_id"],
    ]
    by_id = {s["session_id"]: s for s in sessions}
    assert by_id[a["session_id"]]["status"] == "completed"
    assert by_id[b["session_id"]]["status"] == "upload"
    # 精简摘要：不泄漏 physical_types 等无关字段
    assert "physical_types" not in by_id[a["session_id"]]
