"""AnalysisBundle 路由（重构 Phase 1）：自动多维分析的规划与批量执行。

与既有单问题链路并存、互不干扰；产物写入 bundle/ 独立目录。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from app.schemas.bundle import AnalysisBundle
from app.schemas.dictionary import DataDictionary
from app.services.bundle_executor import execute_bundle
from app.services.bundle_planner import build_bundle
from app.services.storage import SessionStore, StorageError

router = APIRouter(prefix="/sessions/{session_id}", tags=["bundle"])


def _require_inputs(session_id: str, store: SessionStore):
    """语义确认完成 + 质量快照存在，是 Bundle 规划的前置闸门。

    会话不存在 → 404（由调用方按 StorageError 处理）；
    阶段产物缺失/未确认 → 409 并给出可操作中文提示。
    """
    session_dir = store.session_dir(session_id)  # 会话不存在抛 StorageError
    dict_path = session_dir / "dictionary.json"
    if not dict_path.exists():
        raise HTTPException(status_code=409, detail="请先完成数据语义确认。")
    dictionary = DataDictionary.model_validate(json.loads(
        dict_path.read_text(encoding="utf-8")))
    if not dictionary.complete:
        raise HTTPException(status_code=409, detail="数据语义尚未确认完成。")
    if not (session_dir / "snapshot.parquet").exists():
        raise HTTPException(status_code=409, detail="请先完成数据质量处理并生成数据快照。")
    snapshot = store.load_snapshot(session_id)
    meta = store.get_meta(session_id)
    return dictionary, snapshot, meta


@router.post("/analysis-bundle")
def generate(session_id: str) -> dict:
    store = SessionStore()
    try:
        dictionary, snapshot, meta = _require_inputs(session_id, store)
        quality = store.read_artifact(session_id, "quality")
        bundle = build_bundle(
            dictionary,
            snapshot,
            snapshot_rows=int(quality["snapshot_rows"]),
            title=meta.get("filename", session_id),
        )
        store.write_bundle(session_id, bundle.model_dump(mode="json"))
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return bundle.model_dump(mode="json")


@router.get("/analysis-bundle")
def get_bundle(session_id: str) -> dict:
    try:
        return SessionStore().read_bundle(session_id)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/analysis-bundle/execute")
def run_bundle(session_id: str) -> dict:
    store = SessionStore()
    try:
        bundle = AnalysisBundle.model_validate(store.read_bundle(session_id))
        summary = execute_bundle(session_id, store, bundle)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return summary.model_dump(mode="json")


@router.get("/analysis-bundle/execution")
def get_execution(session_id: str) -> dict:
    try:
        return SessionStore().read_bundle_execution(session_id)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
