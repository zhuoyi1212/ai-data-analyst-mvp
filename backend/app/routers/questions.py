"""阶段 4 Question Recommendation 路由。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.recommender import recommend
from app.services.storage import SessionStore, StorageError

router = APIRouter(prefix="/sessions/{session_id}/questions", tags=["questions"])


@router.post("/generate")
def generate(session_id: str, payload: dict | None = None) -> dict:
    variant = int((payload or {}).get("variant", 0))
    fixture_name = (payload or {}).get("fixture_name")
    try:
        qs = recommend(session_id, SessionStore(), variant=variant, fixture_name=fixture_name)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"questions": qs.model_dump(mode="json")["questions"]}


@router.get("")
def get_questions(session_id: str) -> dict:
    try:
        return {"questions": SessionStore().read_artifact(session_id, "questions")["questions"]}
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
