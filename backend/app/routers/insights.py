"""阶段 9 Insight 路由：生成（含门禁）与查询。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.insight import generate_insights
from app.services.storage import SessionStore, StorageError
from app.services.validator import ValidationGateError

router = APIRouter(prefix="/sessions/{session_id}/insights", tags=["insights"])


@router.post("/generate")
def generate(session_id: str, payload: dict | None = None) -> dict:
    fixture_name = (payload or {}).get("fixture_name")
    try:
        insight_set = generate_insights(session_id, SessionStore(), fixture_name=fixture_name)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationGateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"insights": [i.model_dump(mode="json") for i in insight_set.insights]}


@router.get("")
def get(session_id: str) -> dict:
    try:
        return SessionStore().read_artifact(session_id, "insights")
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
