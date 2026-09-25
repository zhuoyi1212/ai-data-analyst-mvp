"""DashboardArtifact 路由（重构 Phase 2）。

POST /sessions/{id}/dashboard：要求 bundle + execution 均已存在（门禁同 bundle），
执行确定性合成（KPI/Sections/Findings/probe），落盘 dashboard.json 后返回完整产物。
GET  /sessions/{id}/dashboard：读回已合成产物。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.dashboard_synthesizer import synthesize_dashboard
from app.services.storage import SessionStore, StorageError

router = APIRouter(prefix="/sessions/{session_id}", tags=["dashboard"])


def _require_bundle_executed(session_id: str, store: SessionStore) -> None:
    if not store.has_bundle(session_id):
        raise HTTPException(status_code=409, detail="请先生成分析蓝图（AnalysisBundle）。")
    if not store.has_bundle_execution(session_id):
        raise HTTPException(
            status_code=409, detail="请先执行分析蓝图（analysis-bundle/execute）。"
        )


@router.post("/dashboard")
def build_dashboard(session_id: str) -> dict:
    store = SessionStore()
    try:
        _require_bundle_executed(session_id, store)
        artifact = synthesize_dashboard(session_id, store)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HTTPException:
        raise
    return artifact.model_dump(mode="json")


@router.get("/dashboard")
def get_dashboard(session_id: str) -> dict:
    try:
        return SessionStore().read_dashboard(session_id)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
