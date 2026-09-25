"""阶段 2 Semantic Profile 路由。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.profiler import confirm_fields, generate_dictionary
from app.services.storage import SessionStore, StorageError

router = APIRouter(prefix="/sessions/{session_id}/profile", tags=["profile"])


@router.post("/generate")
def generate(session_id: str, payload: dict | None = None) -> dict:
    store = SessionStore()
    try:
        fixture_name = (payload or {}).get("fixture_name")
        dictionary = generate_dictionary(session_id, store, fixture_name)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"dictionary": dictionary.model_dump(mode="json")}


@router.get("")
def get_profile(session_id: str) -> dict:
    try:
        return {"dictionary": SessionStore().read_artifact(session_id, "dictionary")}
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/confirm")
def confirm(session_id: str, payload: dict) -> dict:
    decisions = payload.get("fields", [])
    if not isinstance(decisions, list) or not decisions:
        raise HTTPException(status_code=400, detail="请至少提交一个字段的确认结果。")
    try:
        dictionary = confirm_fields(session_id, decisions, SessionStore())
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:  # 枚举值非法
        raise HTTPException(status_code=400, detail=f"确认内容不合法：{exc}") from exc
    return {"dictionary": dictionary.model_dump(mode="json"), "complete": dictionary.complete}
