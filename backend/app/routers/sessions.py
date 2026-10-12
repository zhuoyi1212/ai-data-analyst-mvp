"""会话与上传路由（阶段 1 Upload）。"""
from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, HTTPException, UploadFile

from app.config import settings
from app.services.parser import UploadError, parse_table
from app.services.storage import SessionStore, StorageError

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _store() -> SessionStore:
    return SessionStore()


@router.get("/samples")
def list_samples() -> dict:
    if not settings.sample_data_dir.exists():
        return {"samples": []}
    return {
        "samples": [
            p.name
            for p in sorted(settings.sample_data_dir.iterdir())
            if p.suffix.lower() in {".csv", ".xlsx", ".xls"}
        ]
    }


@router.post("/upload")
async def upload(file: UploadFile) -> dict:
    data = await file.read()
    try:
        meta = _store().create_from_bytes(file.filename or "upload.csv", data)
    except UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"meta": meta, "preview": _store().read_artifact(meta["session_id"], "preview")}


@router.post("/from-sample")
def from_sample(payload: dict) -> dict:
    name = payload.get("filename", "")
    try:
        meta = _store().create_from_sample(name)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"meta": meta, "preview": _store().read_artifact(meta["session_id"], "preview")}


@router.get("")
def list_sessions() -> dict:
    store = _store()
    summaries = [
        _session_summary(store, meta) for meta in store.list_sessions()
    ]
    summaries.sort(key=lambda m: m["created_at"], reverse=True)
    return {"sessions": summaries}


def _session_summary(store: SessionStore, meta: dict) -> dict:
    """把原始 meta 精简为列表摘要，并推导分析状态（auto.json 的 status）。"""
    sid = meta.get("session_id", "")
    status = "upload"
    if store.has_artifact(sid, "auto"):
        try:
            auto = store.read_artifact(sid, "auto")
            status = auto.get("status") or "upload"
        except StorageError:
            status = "upload"
    return {
        "session_id": sid,
        "filename": meta.get("filename", ""),
        "created_at": meta.get("created_at", ""),
        "shape": meta.get("shape", {"rows": 0, "cols": 0}),
        "source_sample": meta.get("source_sample"),
        "status": status,
    }


@router.delete("/{session_id}")
def delete_session(session_id: str) -> dict:
    try:
        _store().delete_session(session_id)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": session_id}


@router.get("/{session_id}")
def get_session(session_id: str) -> dict:
    try:
        store = _store()
        return {"meta": store.get_meta(session_id)}
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{session_id}/preview")
def get_preview(session_id: str) -> dict:
    try:
        return _store().read_artifact(session_id, "preview")
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
