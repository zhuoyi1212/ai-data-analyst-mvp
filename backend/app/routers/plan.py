"""阶段 5 Analysis Plan 路由：生成 / 编辑重校验 / 确认锁定。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.planner import confirm_plan, edit_plan, generate_plan
from app.services.storage import SessionStore, StorageError

router = APIRouter(prefix="/sessions/{session_id}/plan", tags=["plan"])


@router.post("/generate")
def generate(session_id: str, payload: dict) -> dict:
    question = (payload or {}).get("question", "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="请提供要分析的问题。")
    try:
        artifact = generate_plan(
            session_id,
            question,
            SessionStore(),
            fixture_name=payload.get("fixture_name"),
            source_question_index=payload.get("source_question_index"),
        )
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return artifact.model_dump(mode="json")


@router.get("")
def get_plan(session_id: str) -> dict:
    try:
        return SessionStore().read_artifact(session_id, "plan")
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/edit")
def edit(session_id: str, payload: dict) -> dict:
    raw = (payload or {}).get("plan")
    if not raw:
        raise HTTPException(status_code=422, detail="缺少 plan 字段。")
    try:
        artifact = edit_plan(session_id, raw, SessionStore())
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return artifact.model_dump(mode="json")


@router.post("/confirm")
def confirm(session_id: str, payload: dict | None = None) -> dict:
    raw = (payload or {}).get("plan") if payload else None
    try:
        artifact = confirm_plan(session_id, SessionStore(), raw)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return artifact.model_dump(mode="json")
