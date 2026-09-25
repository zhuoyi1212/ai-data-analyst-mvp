"""TR-5.1 / TR-5.2 / TR-5.3：语义识别、确认闸门、落盘字典。"""
from __future__ import annotations

import pandas as pd
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.common import Confidence, SemanticType
from app.services.profiler import confirm_fields, generate_dictionary
from app.services.storage import SessionStore


def _df(n: int = 120) -> pd.DataFrame:
    qty = [1 + i % 5 for i in range(n)]
    price = [10.0 + (i % 4) * 2.5 for i in range(n)]
    dates = [
        f"2024-{((i % 12) + 1):02d}-{(i % 27) + 1:02d}"
        if i % 2 == 0
        else f"2024/{(i % 12) + 1}/{(i % 27) + 1}"
        for i in range(n)
    ]
    return pd.DataFrame(
        {
            "订单号": [f"A{i:05d}" for i in range(n)],
            "日期": dates,
            "区域": [["华北", "华东", "华南"][i % 3] for i in range(n)],
            "数量": qty,
            "单价": price,
            "金额": [q * p for q, p in zip(qty, price)],
            "备注": [f"客户留言内容编号{i}" for i in range(n)],
        }
    )


_FIXTURE = {
    "fields": [
        {"name": "订单号", "semantic_type": "id", "meaning": "销售订单唯一编号",
         "candidates": [], "confidence": "high"},
        {"name": "日期", "semantic_type": "date", "meaning": "订单成交日期",
         "candidates": [], "confidence": "high"},
        {"name": "区域", "semantic_type": "dimension", "meaning": "销售所属大区",
         "candidates": ["大区", "片区"], "confidence": "high"},
        {"name": "数量", "semantic_type": "metric", "meaning": "成交件数",
         "unit": "件", "candidates": [], "confidence": "high"},
        {"name": "单价", "semantic_type": "metric", "meaning": "商品成交单价",
         "unit": "元", "candidates": [], "confidence": "high"},
        {"name": "金额", "semantic_type": "metric", "meaning": "订单成交金额",
         "unit": "元", "candidates": [], "confidence": "high"},
        {"name": "备注", "semantic_type": "unknown", "meaning": "含义不明的文本字段",
         "candidates": ["客户备注", "订单标记"], "confidence": "low"},
    ]
}


def _setup(tmp_path, llm_env):
    llm_env("profile", "mini", _FIXTURE)
    store = SessionStore(tmp_path / "storage")
    meta = store.create_from_bytes("mini_sales.csv", _df().to_csv(index=False).encode())
    return store, meta["session_id"]


def test_semantic_detection_and_gate(tmp_path, llm_env):
    store, sid = _setup(tmp_path, llm_env)
    dictionary = generate_dictionary(sid, store, fixture_name="mini")
    by = {f.name: f for f in dictionary.fields}

    assert by["日期"].semantic_type == SemanticType.date
    assert by["日期"].confidence == Confidence.high
    assert by["订单号"].semantic_type == SemanticType.id
    assert by["金额"].semantic_type == SemanticType.metric
    assert by["区域"].semantic_type == SemanticType.dimension

    assert by["备注"].semantic_type == SemanticType.unknown
    assert by["备注"].confidence == Confidence.low
    assert dictionary.complete is False

    # 乘积关系被确定性识别
    product = next(r for r in dictionary.relations if r.type == "product")
    assert set(product.columns) == {"数量", "单价", "金额"}


def test_confirm_unblocks_gate_and_excludes_ignored(tmp_path, llm_env):
    store, sid = _setup(tmp_path, llm_env)
    generate_dictionary(sid, store, fixture_name="mini")

    dictionary = confirm_fields(sid, [{"name": "备注", "ignored": True}], store)
    assert dictionary.complete is True

    usable = {f.name for f in dictionary.usable_field_infos()}
    assert "备注" not in usable
    assert dictionary.date_fields() == ["日期"]

    persisted = store.read_artifact(sid, "dictionary")
    assert persisted["complete"] is True
    note_field = next(f for f in persisted["fields"] if f["name"] == "备注")
    assert note_field["ignored"] is True and note_field["confirmed_by_user"] is True


def test_profile_api_flow(tmp_path, llm_env, monkeypatch):
    store, sid = _setup(tmp_path, llm_env)
    monkeypatch.setattr("app.routers.sessions._store", lambda: store)
    monkeypatch.setattr("app.routers.profile.SessionStore", lambda: store)
    client = TestClient(app)

    resp = client.post(f"/sessions/{sid}/profile/generate", json={"fixture_name": "mini"})
    assert resp.status_code == 200
    assert resp.json()["dictionary"]["complete"] is False

    resp2 = client.post(
        f"/sessions/{sid}/profile/confirm", json={"fields": [{"name": "备注", "ignored": True}]}
    )
    assert resp2.status_code == 200 and resp2.json()["complete"] is True
