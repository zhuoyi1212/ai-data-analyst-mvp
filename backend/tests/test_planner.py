"""TR-9.1/9.2/9.3：方案两层校验、有限修复、确认锁定与编辑拦截。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.common import Confidence, SemanticType
from app.schemas.dictionary import DataDictionary, FieldProfile
from app.schemas.plan import AnalysisPlan
from app.services.llm.errors import ContractError
from app.services.planner import confirm_plan, edit_plan, generate_plan, plan_hash
from app.services.storage import SessionStore


def _field(name, st):
    return FieldProfile(
        name=name, physical_type="string", semantic_type=st,
        confidence=Confidence.high, confirmed=True, confirmed_by_user=True,
    )


def _dictionary(complete=True) -> DataDictionary:
    return DataDictionary(
        session_id="x", complete=complete,
        fields=[_field("日期", SemanticType.date), _field("区域", SemanticType.dimension),
                _field("品类", SemanticType.dimension), _field("金额", SemanticType.metric),
                _field("数量", SemanticType.metric)],
    )


def _plan(column="金额", op="aggregate", **extra):
    steps = [{"step_id": "s1", "op": op,
              "params": {"column": column, "func": "sum", **extra},
              "description": "对金额求和", "depends_on": []}]
    return {"question": "总销售额是多少", "data_scope": "全量数据", "steps": steps}


def _session(tmp_path, dictionary=None):
    import pandas as pd

    store = SessionStore(tmp_path / "storage")
    d = dictionary or _dictionary()
    cols = {f.name: (pd.Timestamp("2024-01-01") if f.semantic_type == SemanticType.date
                     else (["x"] if f.semantic_type != SemanticType.metric else [1.0]))
            for f in d.fields}
    sid = store.create_from_bytes("d.csv", pd.DataFrame(cols).to_csv(index=False).encode())["session_id"]
    store.write_artifact(sid, "dictionary",
                         {**d.model_dump(mode="json"), "session_id": sid})
    return store, sid


def test_valid_plan_generates_and_locks(tmp_path, llm_env):
    llm_env("plan", "mini_plan", _plan())
    store, sid = _session(tmp_path)
    art = generate_plan(sid, "总销售额是多少", store, fixture_name="mini")
    assert art.locked is False and art.plan_hash is None
    assert art.chart.value == "metric"
    locked = confirm_plan(sid, store)
    assert locked.locked is True
    assert len(locked.plan_hash) == 16
    assert locked.plan_hash == plan_hash(AnalysisPlan.model_validate(_plan()))
    assert store.get_meta(sid)["stage"] == "plan"


def test_business_error_repaired_within_two_attempts(tmp_path, llm_env):
    llm_env("plan", "mini_plan", {"responses": [_plan("不存在的字段"), _plan()]})
    store, sid = _session(tmp_path)
    art = generate_plan(sid, "总销售额是多少", store, fixture_name="mini")
    assert art.plan.steps[0].params.column == "金额"


def test_business_error_exhausted_raises(tmp_path, llm_env):
    bad_time = {
        "question": "区域的时间趋势", "data_scope": "全量",
        "steps": [{"step_id": "s1", "op": "time_series",
                   "params": {"date_column": "区域", "granularity": "month",
                              "metric": "金额", "func": "sum"},
                   "description": "按月趋势", "depends_on": []}],
    }
    llm_env("plan", "mini_plan", {"responses": [bad_time, bad_time, bad_time]})
    store, sid = _session(tmp_path)
    with pytest.raises(ContractError) as exc:
        generate_plan(sid, "区域的时间趋势", store, fixture_name="mini")
    assert any("日期字段" in r for r in exc.value.reasons)


def test_unknown_op_contract_error(tmp_path, llm_env):
    bad = {"question": "q", "steps": [
        {"step_id": "s1", "op": "run_python", "params": {"code": "1+1"},
         "description": "x", "depends_on": []}]}
    llm_env("plan", "mini_plan", {"responses": [bad, bad, bad]})
    store, sid = _session(tmp_path)
    with pytest.raises(ContractError):
        generate_plan(sid, "q", store, fixture_name="mini")


def test_edit_with_ghost_field_rejected(tmp_path, llm_env):
    llm_env("plan", "mini_plan", _plan())
    store, sid = _session(tmp_path)
    generate_plan(sid, "总销售额是多少", store, fixture_name="mini")
    with pytest.raises(ContractError) as exc:
        edit_plan(sid, _plan("幽灵列"), store)
    assert any("幽灵列" in r for r in exc.value.reasons)
    # 草稿保持未锁定
    assert store.read_artifact(sid, "plan")["locked"] is False


def test_generate_blocked_before_profile_complete(tmp_path, monkeypatch, llm_env):
    llm_env("plan", "mini_plan", _plan())
    store, _ = _session(tmp_path, _dictionary(complete=False))
    from app.routers import plan as plan_router

    monkeypatch.setattr(plan_router, "SessionStore", lambda: store)
    client = TestClient(app)
    # 取一个真实 session id
    sid = store.list_sessions()[0]["session_id"]
    resp = client.post(f"/sessions/{sid}/plan/generate", json={"question": "总销售额"})
    assert resp.status_code == 409
    assert "语义确认" in resp.json()["detail"]
