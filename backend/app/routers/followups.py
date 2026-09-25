"""阶段 10 Follow-up 路由。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.followup import generate_followups
from app.services.storage import SessionStore, StorageError
from app.services.validator import ValidationGateError

router = APIRouter(prefix="/sessions/{session_id}/followups", tags=["followup"])


@router.post("/generate")
def generate(session_id: str, payload: dict | None = None) -> dict:
    fixture_name = (payload or {}).get("fixture_name")
    try:
        followups = generate_followups(session_id, SessionStore(), fixture_name=fixture_name)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationGateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return followups.model_dump(mode="json")


@router.get("")
def get(session_id: str) -> dict:
    try:
        return SessionStore().read_artifact(session_id, "followups")
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
