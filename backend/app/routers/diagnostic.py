"""DiagnosticSearch 路由（P1 T09）。

POST /sessions/{id}/diagnostic-search：
    无 search → 新建并在预算内跑到收敛；running → 续跑（预算不重置）；
    completed → 原样返回（幂等）。search 属于旧运行版本时 409 stale。
GET  /sessions/{id}/diagnostic-search：读取当前运行上的诊断状态。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas.diagnostic import DiagnosticSearch
from app.services.diagnostic_search import start_diagnostic_search
from app.services.storage import StaleRunError, StorageError

router = APIRouter(prefix="/sessions/{session_id}", tags=["diagnostic"])


@router.post("/diagnostic-search")
def run_diagnostic_search(session_id: str) -> dict:
    from app.services.storage import SessionStore

    store = SessionStore()
    try:
        search = start_diagnostic_search(session_id, store)
    except StaleRunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return search.model_dump(mode="json")


@router.get("/diagnostic-search")
def get_diagnostic_search(session_id: str) -> dict:
    from app.services.storage import SessionStore

    store = SessionStore()
    try:
        payload = store.read_diagnostic(session_id)
        search = DiagnosticSearch.model_validate(payload)
        active_rid = store.active_run_id(session_id)
        if active_rid is not None and search.run_id != active_rid:
            raise StaleRunError(
                f"诊断搜索属于旧运行版本（{search.run_id}），"
                f"当前工作流运行是 {active_rid}；请在当前运行上重新发起。"
            )
    except StaleRunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return search.model_dump(mode="json")
