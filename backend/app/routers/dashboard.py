"""DashboardArtifact 路由（P0 T01/T04）。

POST /sessions/{id}/dashboard：当前运行必须已执行；版本指纹校验通过才合成，
    同运行重复请求幂等；错配一律 409 stale。
GET  /sessions/{id}/dashboard：只返回 current 指针的已发布版本；
    已发布版本不是当前运行（active≠current）时返回 409 stale，旧版可读但不冒充最新。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.dashboard_synthesizer import synthesize_dashboard
from app.services.storage import SessionStore, StaleRunError, StorageError

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
    except StaleRunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HTTPException:
        raise
    return artifact.model_dump(mode="json")


@router.get("/dashboard")
def get_dashboard(session_id: str) -> dict:
    store = SessionStore()
    try:
        if store.is_dashboard_stale(session_id):
            cur = store.current_run_id(session_id)
            act = store.active_run_id(session_id)
            raise StaleRunError(
                f"已发布仪表盘属于旧运行版本（{cur}），当前工作流运行是 {act}，"
                "请重新执行并合成后再查看；旧版本仍可审计。"
            )
        return store.read_dashboard(session_id)
    except StaleRunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
