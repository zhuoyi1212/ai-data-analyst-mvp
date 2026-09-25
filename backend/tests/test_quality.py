"""TR-6.1 / TR-7.1 / TR-7.2：质量检测、处理动作、快照与闸门。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.common import Confidence, SemanticType
from app.schemas.dictionary import DataDictionary, FieldProfile
from app.schemas.quality import QualityReport
from app.services.quality_actions import apply_decisions
from app.services.quality_checker import run_checks, run_quality_checks
from app.services.storage import SessionStore


def _df(n: int = 100, rng_seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(rng_seed)
    region = np.array([["华北", "华东", "华南"][i % 3] for i in range(n)], dtype=object)
    region[2] = None
    region[55] = None
    region[88] = None
    channel = np.array(["Web"] * n, dtype=object)
    channel[10:60] = "WeChat"
    channel[12] = "wechat"
    channel[40] = "wechat"
    dau = rng.integers(200, 800, n).astype(float)
    dau[[4, 30, 77]] = -1
    amount = rng.integers(80, 200, n).astype(float)
    amount[66] = 99999.0
    spend = [f"¥{1000 + i:,.2f}" for i in range(n)]
    dt = [f"2024-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}" for i in range(n)]
    df = pd.DataFrame(
        {
            "tid": [f"T{i:04d}" for i in range(n)],
            "dt": dt,
            "region": region,
            "channel": channel,
            "spend": spend,
            "dau": dau,
            "amount": amount,
        }
    )
    return pd.concat([df, df.iloc[[0, 1]]], ignore_index=True)  # 2 行完全重复


def _dictionary() -> DataDictionary:
    specs = [
        ("tid", "string", SemanticType.id),
        ("dt", "string", SemanticType.date),
        ("region", "string", SemanticType.dimension),
        ("channel", "string", SemanticType.dimension),
        ("spend", "string", SemanticType.metric),
        ("dau", "float", SemanticType.metric),
        ("amount", "float", SemanticType.metric),
    ]
    fields = [
        FieldProfile(
            name=n, physical_type=p, semantic_type=t, confidence=Confidence.high,
            confirmed=True, confirmed_by_user=True,
        )
        for n, p, t in specs
    ]
    return DataDictionary(session_id="x", fields=fields, complete=True)


def _issue_ids(report: QualityReport) -> set[str]:
    return {i.issue_id for i in report.issues}


def test_detect_all_four_issue_families():
    report_issues = run_checks(_df(), _dictionary())
    ids = _issue_ids(QualityReport(session_id="x", issues=report_issues))
    assert "missing:region" in ids
    assert "missing:dau" in ids
    assert "format:sentinel:dau" in ids
    assert "format:date_text:dt" in ids
    assert "format:currency:spend" in ids
    assert "format:enum_case:channel" in ids
    assert "duplicate:rows" in ids
    assert "outlier:amount" in ids

    dup = next(i for i in report_issues if i.issue_id == "duplicate:rows")
    assert dup.evidence["extra_rows"] == 2
    outlier = next(i for i in report_issues if i.issue_id == "outlier:amount")
    assert outlier.evidence["count"] == 1
    sentinel = next(i for i in report_issues if i.issue_id == "format:sentinel:dau")
    assert sentinel.evidence["count"] == 3


def _all_decisions(overrides: dict | None = None) -> dict:
    decisions = {
        "missing:region": {"action": "fill_mode"},
        "missing:dau": {"action": "fill_median"},
        "format:date_text:dt": {"action": "convert"},
        "format:currency:spend": {"action": "convert"},
        "format:enum_case:channel": {"action": "convert"},
        "format:sentinel:dau": {"action": "convert"},
        "duplicate:rows": {"action": "drop_duplicates"},
        "outlier:amount": {"action": "keep"},
    }
    if overrides:
        decisions.update(overrides)
    return decisions


def _session(tmp_path) -> tuple[SessionStore, str]:
    store = SessionStore(tmp_path / "storage")
    df = _df()
    meta = store.create_from_bytes("q.csv", df.to_csv(index=False).encode("utf-8-sig"))
    sid = meta["session_id"]
    dictionary = _dictionary()
    dictionary.session_id = sid
    store.write_artifact(sid, "dictionary", dictionary.model_dump(mode="json"))
    run_quality_checks(sid, store)
    return store, sid


def test_apply_actions_builds_snapshot(tmp_path):
    store, sid = _session(tmp_path)
    report = apply_decisions(sid, _all_decisions(), store)
    assert report.complete is True
    assert report.snapshot_rows == 100  # 102 - 2 重复行

    snap = store.load_snapshot(sid)
    assert str(snap["spend"].dtype).startswith("float")
    assert abs(snap["spend"].iloc[0] - 1000.0) < 1e-6
    assert str(snap["dt"].dtype).startswith("datetime64")
    assert set(snap["channel"].dropna().unique()) <= {"Web", "WeChat"}
    assert int((snap["dau"] == -1).sum()) == 0
    assert snap["amount"].max() == 99999.0  # keep：离群保留
    assert report.snapshot_hash and len(report.snapshot_hash) == 64


def test_outlier_exclude_and_mark(tmp_path):
    store, sid = _session(tmp_path)
    report = apply_decisions(
        sid, _all_decisions({"outlier:amount": {"action": "exclude"}}), store
    )
    assert report.snapshot_rows == 99
    snap = store.load_snapshot(sid)
    assert snap["amount"].max() < 99999.0

    store2, sid2 = _session(tmp_path)
    apply_decisions(
        sid2, _all_decisions({"outlier:amount": {"action": "mark"}}), store2
    )
    snap2 = store2.load_snapshot(sid2)
    assert "amount_is_outlier" in snap2.columns
    assert int(snap2["amount_is_outlier"].sum()) == 1


def test_incomplete_decisions_rejected(tmp_path):
    store, sid = _session(tmp_path)
    with pytest.raises(ValueError, match="未做出处理决策"):
        apply_decisions(sid, {"missing:region": {"action": "keep"}}, store)
    # 未生成快照
    assert not (store.session_dir(sid) / "snapshot.parquet").exists()


def test_quality_api(tmp_path, monkeypatch):
    store, sid = _session(tmp_path)
    monkeypatch.setattr("app.routers.quality.SessionStore", lambda: store)
    client = TestClient(app)

    resp = client.get(f"/sessions/{sid}/quality")
    assert resp.status_code == 200 and len(resp.json()["report"]["issues"]) >= 8

    resp2 = client.post(f"/sessions/{sid}/quality/apply", json={"decisions": _all_decisions()})
    assert resp2.status_code == 200
    assert resp2.json()["report"]["snapshot_rows"] == 100
