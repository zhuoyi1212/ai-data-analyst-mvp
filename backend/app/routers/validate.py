"""阶段 7 Validate 路由：运行五项校验 / 查询 / 知悉 warn。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.engine.ops import EngineError
from app.services.storage import SessionStore, StorageError
from app.services.validator import (
    ValidationGateError,
    acknowledge,
    ensure_consumable,
    run_validation,
)

router = APIRouter(prefix="/sessions/{session_id}/validate", tags=["validate"])


@router.post("")
def run(session_id: str) -> dict:
    try:
        report = run_validation(session_id, SessionStore())
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return report.model_dump(mode="json")


@router.get("")
def get(session_id: str) -> dict:
    try:
        return SessionStore().read_artifact(session_id, "validation")
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/acknowledge")
def ack(session_id: str) -> dict:
    try:
        report = acknowledge(session_id, SessionStore())
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationGateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return report.model_dump(mode="json")
