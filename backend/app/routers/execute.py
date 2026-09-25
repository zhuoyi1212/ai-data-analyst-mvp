"""阶段 6 Execute 路由：执行锁定方案并返回计算台账。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.engine.ops import EngineError
from app.services.executor import ExecutionGateError, execute
from app.services.storage import SessionStore, StorageError

router = APIRouter(prefix="/sessions/{session_id}", tags=["execute"])


@router.post("/execute")
def run(session_id: str) -> dict:
    try:
        ledger = execute(session_id, SessionStore())
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExecutionGateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ledger.model_dump(mode="json")


@router.get("/ledger")
def get_ledger(session_id: str) -> dict:
    try:
        return SessionStore().read_artifact(session_id, "ledger")
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
