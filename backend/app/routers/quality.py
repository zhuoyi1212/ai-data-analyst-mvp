"""阶段 3 Data Quality 路由（检测 + 处理决策 + 快照）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.quality_actions import apply_decisions
from app.services.quality_checker import run_quality_checks
from app.services.storage import SessionStore, StorageError

router = APIRouter(prefix="/sessions/{session_id}/quality", tags=["quality"])


@router.post("/run")
def run(session_id: str) -> dict:
    try:
        report = run_quality_checks(session_id, SessionStore())
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"report": report.model_dump(mode="json")}


@router.get("")
def get_report(session_id: str) -> dict:
    try:
        return {"report": SessionStore().read_artifact(session_id, "quality")}
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/apply")
def apply(session_id: str, payload: dict) -> dict:
    decisions = payload.get("decisions", {})
    if not isinstance(decisions, dict):
        raise HTTPException(status_code=400, detail="decisions 必须是以 issue_id 为键的对象。")
    try:
        report = apply_decisions(session_id, decisions, SessionStore())
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"report": report.model_dump(mode="json")}
