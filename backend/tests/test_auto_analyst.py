"""Task 1：Autonomous Analyst 编排骨架与智能 Gate 测试。

覆盖验收：
- TR-1.1 正常数据一键直达 Dashboard
- TR-1.2 五类数据的放行/拦截（正常/无指标/零行/普通质量问题/关键歧义）
- TR-1.3 Gate 作答断点续跑、幂等不重复产 run
- TR-1.4 保守默认决策与快照行数一致
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.schemas.common import Confidence, SemanticType
from app.schemas.dictionary import DataDictionary, FieldProfile
from app.schemas.quality import QualityReport
from app.services.auto_analyst import start_auto_analysis
from app.services.storage import SessionStore

client = TestClient(app)


# ------------------------------------------------------------ 夹具


@pytest.fixture
def patched_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionStore:
    """让路由/服务内部的 SessionStore() 落到临时目录，返回该 store。"""
    test_settings = dataclasses.replace(settings, storage_dir=tmp_path / "store")
    import app.services.storage as storage_mod

    monkeypatch.setattr(storage_mod, "settings", test_settings)
    return SessionStore()


def _field(
    name: str,
    physical: str,
    semantic: SemanticType,
    *,
    confidence: Confidence = Confidence.high,
) -> FieldProfile:
    return FieldProfile(
        name=name,
        physical_type=physical,
        semantic_type=semantic,
        confidence=confidence,
        confirmed=confidence is Confidence.high,
        is_metric=semantic is SemanticType.metric,
    )


def _write_dictionary(
    store: SessionStore, sid: str, fields: list[FieldProfile], *, complete: bool
) -> None:
    dictionary = DataDictionary(session_id=sid, fields=fields, complete=complete)
    store.write_artifact(sid, "dictionary", dictionary.model_dump(mode="json"))


def _upload(store: SessionStore, csv_text: str, name: str = "data.csv") -> str:
    return store.create_from_bytes(name, csv_text.encode("utf-8"))["session_id"]


# ------------------------------------------------------------ TR-1.1


def test_one_click_normal_sample_completes_to_dashboard(patched_store: SessionStore):
    store = patched_store
    sid = store.create_from_sample("sales_orders.csv")["session_id"]

    state = start_auto_analysis(sid, store)

    assert state.status == "completed"
    assert state.run_id
    assert state.current_stage == "synthesis"
    statuses = {s.stage: s.status for s in state.stages}
    assert statuses["profile"] == "completed"
    assert statuses["quality"] == "completed"
    assert statuses["synthesis"] == "completed"
    # scan/signals/diagnostic 已启用（无 top 信号时 diagnostic 允许 skipped）
    assert statuses["scan"] == "completed"
    assert statuses["signals"] == "completed"
    assert statuses["diagnostic"] in ("completed", "skipped")
    # Dashboard 产物随 run 版本化存储（报告在 Task 6 接入）
    assert store.read_dashboard(sid) is not None


def test_api_get_state_route(patched_store: SessionStore):
    store = patched_store
    sid = store.create_from_sample("sales_orders.csv")["session_id"]
    resp = client.post(f"/sessions/{sid}/auto-analyze", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"

    got = client.get(f"/sessions/{sid}/auto-analyze")
    assert got.status_code == 200
    assert got.json()["run_id"] == resp.json()["run_id"]


def test_api_unknown_session_404():
    resp = client.post("/sessions/no-such-session/auto-analyze", json={})
    assert resp.status_code == 404


# ------------------------------------------------------------ TR-1.2 五类数据


def test_no_metric_confident_dimensions_fails(patched_store: SessionStore):
    store = patched_store
    sid = _upload(
        store,
        "name,city\nAlice,Beijing\nBob,Shanghai\nCarol,Beijing\n",
    )
    _write_dictionary(
        store,
        sid,
        [
            _field("name", "string", SemanticType.dimension),
            _field("city", "string", SemanticType.dimension),
        ],
        complete=True,
    )

    state = start_auto_analysis(sid, store)

    assert state.status == "failed"
    assert state.current_stage == "profile"
    assert "没有可分析的数值指标" in (state.error or "")


def test_no_metric_api_returns_422(patched_store: SessionStore):
    store = patched_store
    sid = _upload(store, "name,city\nAlice,Beijing\nBob,Shanghai\n")
    _write_dictionary(
        store,
        sid,
        [
            _field("name", "string", SemanticType.dimension),
            _field("city", "string", SemanticType.dimension),
        ],
        complete=True,
    )
    resp = client.post(f"/sessions/{sid}/auto-analyze", json={})
    assert resp.status_code == 422
    assert "没有可分析的数值指标" in resp.json()["detail"]


def test_zero_row_snapshot_fails(patched_store: SessionStore):
    store = patched_store
    sid = _upload(store, "amount\n100\n")
    _write_dictionary(
        store, sid, [_field("amount", "integer", SemanticType.metric)], complete=True
    )
    # 已有质量决策但快照为空（极端严重数据错误）
    report = QualityReport(
        session_id=sid, issues=[], complete=True, snapshot_rows=0, snapshot_hash="x"
    )
    store.write_artifact(sid, "quality", report.model_dump(mode="json"))
    store.save_snapshot(sid, pd.DataFrame(columns=["amount"]))

    state = start_auto_analysis(sid, store)

    assert state.status == "failed"
    assert state.current_stage == "quality"
    assert "没有任何有效行" in (state.error or "")


def test_ordinary_quality_issues_auto_pass(patched_store: SessionStore):
    """sales_orders 含哨兵/重复等普通问题：保守默认直接放行到 Dashboard。"""
    store = patched_store
    sid = store.create_from_sample("sales_orders.csv")["session_id"]

    state = start_auto_analysis(sid, store)

    assert state.status == "completed"
    report = QualityReport.model_validate(store.read_artifact(sid, "quality"))
    assert report.complete
    assert report.snapshot_rows and report.snapshot_rows > 0


def test_critical_ambiguity_asks_gate_question(patched_store: SessionStore):
    store = patched_store
    sid = _upload(
        store,
        "code,city\n100,Beijing\n200,Shanghai\n150,Beijing\n300,Shenzhen\n",
    )
    _write_dictionary(
        store,
        sid,
        [
            _field("code", "integer", SemanticType.unknown, confidence=Confidence.low),
            _field("city", "string", SemanticType.dimension),
        ],
        complete=False,
    )

    state = start_auto_analysis(sid, store)

    assert state.status == "waiting_input"
    assert state.current_stage == "profile"
    assert len(state.needs_input) == 1
    question = state.needs_input[0]
    assert question.question_id == "profile:code"
    assert question.stage == "profile"
    assert question.options == ["metric", "dimension", "date", "ignore"]


# ------------------------------------------------------------ TR-1.3 续跑 + 幂等


def test_gate_answer_resumes_to_completion(patched_store: SessionStore):
    store = patched_store
    sid = _upload(
        store,
        "code,city\n100,Beijing\n200,Shanghai\n150,Beijing\n300,Shenzhen\n"
        "250,Guangzhou\n180,Beijing\n220,Shanghai\n",
    )
    _write_dictionary(
        store,
        sid,
        [
            _field("code", "integer", SemanticType.unknown, confidence=Confidence.low),
            _field("city", "string", SemanticType.dimension),
        ],
        complete=False,
    )

    first = start_auto_analysis(sid, store)
    assert first.status == "waiting_input"

    second = start_auto_analysis(sid, store, answers={"profile:code": "metric"})

    assert second.status == "completed"
    assert second.run_id
    # profile 阶段只执行一次（续跑不重放）
    profile_logs = [s for s in second.stages if s.stage == "profile"]
    assert len(profile_logs) == 1
    assert profile_logs[0].status == "completed"
    # 作答已写入字典：code 成为指标
    dictionary = DataDictionary.model_validate(store.read_artifact(sid, "dictionary"))
    code = dictionary.field("code")
    assert code is not None
    assert code.semantic_type is SemanticType.metric
    assert code.confirmed_by_user
    assert store.read_dashboard(sid) is not None


def test_resume_rejects_unknown_question_id(patched_store: SessionStore):
    store = patched_store
    sid = _upload(
        store,
        "code,city\n100,Beijing\n200,Shanghai\n150,Beijing\n300,Shenzhen\n",
    )
    _write_dictionary(
        store,
        sid,
        [
            _field("code", "integer", SemanticType.unknown, confidence=Confidence.low),
            _field("city", "string", SemanticType.dimension),
        ],
        complete=False,
    )
    start_auto_analysis(sid, store)

    with pytest.raises(ValueError, match="未提出的问题 ID"):
        start_auto_analysis(sid, store, answers={"profile:other": "metric"})


def test_completed_run_is_idempotent(patched_store: SessionStore):
    store = patched_store
    sid = store.create_from_sample("sales_orders.csv")["session_id"]

    first = start_auto_analysis(sid, store)
    second = start_auto_analysis(sid, store)

    assert first.status == "completed"
    assert second.status == "completed"
    assert first.run_id == second.run_id
    import app.services.storage as storage_mod

    run_roots = list(
        (storage_mod.settings.storage_dir / sid / "bundle" / "runs").glob("run_*")
    )
    assert len(run_roots) == 1


# ------------------------------------------------------------ TR-1.4


def test_safe_defaults_snapshot_rows_match_report(patched_store: SessionStore):
    store = patched_store
    sid = store.create_from_sample("sales_orders.csv")["session_id"]

    state = start_auto_analysis(sid, store)

    assert state.status == "completed"
    report = QualityReport.model_validate(store.read_artifact(sid, "quality"))
    snapshot = store.load_snapshot(sid)
    assert report.snapshot_rows == len(snapshot.index)
    # 保守路径不丢弃行（重复/缺失一律保留）：行数等于原始数据
    original = store.load_original(sid)
    assert report.snapshot_rows == len(original.index)
