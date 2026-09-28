"""DashboardArtifact 路由（P0 T01/T04，P1 T06）。

POST /sessions/{id}/dashboard：当前运行必须已执行；版本指纹校验通过才合成，
    同运行重复请求幂等；错配一律 409 stale。
GET  /sessions/{id}/dashboard：只返回 current 指针的已发布版本；
    已发布版本不是当前运行（active≠current）时返回 409 stale，旧版可读但不冒充最新。

T06 新增（浏览器只靠 API 即可绘图，不接触服务器 parquet）：
POST .../dashboard/refine          全局筛选改变 → 重算整批结果并原子发布
GET  .../runs                      历史运行列表
GET  .../runs/{run_id}/dashboard   历史运行的仪表盘（审计只读）
GET  .../runs/{run_id}/views/{vid}/rows   明细 data_ref 翻页
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas.bundle import AnalysisBundle
from app.schemas.dashboard import (
    DashboardArtifact,
    RefineRequest,
    ViewDataEnvelope,
)
from app.services.bundle_executor import execute_bundle
from app.services.bundle_planner import normalize_scope, refine_bundle
from app.services.dashboard_synthesizer import synthesize_dashboard
from app.services.storage import SessionStore, StaleRunError, StorageError
from app.services.view_data import (
    build_columns,
    ordered_detail,
    slice_page,
)

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
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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


# ----------------------------------------------------------------- T06 筛选重算

@router.post("/dashboard/refine")
def refine_dashboard(session_id: str, request: RefineRequest) -> dict:
    """全局筛选改变 → 新运行版本重算聚合/比率/Findings，原子发布。

    不在已聚合数据上隐藏条形；每个 View 重新执行，KPI/图表/Finding
    全部在同一筛选范围内。filters 为空列表即回到全量快照范围。
    """
    store = SessionStore()
    try:
        if not store.has_bundle(session_id):
            raise HTTPException(
                status_code=409,
                detail="请先生成分析蓝图（AnalysisBundle）后再使用全局筛选。",
            )
        snapshot = store.load_snapshot(session_id)
        source = AnalysisBundle.model_validate(store.read_bundle(session_id))
        raw = [f.model_dump(mode="json") for f in request.filters]
        # 先按快照实际列清洗口径，再注入与固化（manifest.scope 只含有效筛选）
        norm = normalize_scope(snapshot, raw)
        clean_scope = [
            {"column": column, "values": [str(v) for v in values]}
            for column, values in norm
        ]
        new_bundle = refine_bundle(source, snapshot, clean_scope)
        store.write_bundle(
            session_id, new_bundle.model_dump(mode="json"),
            scope_filters=clean_scope,
        )
        execute_bundle(session_id, store, new_bundle)
        artifact = synthesize_dashboard(session_id, store)
    except StaleRunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        raise
    return artifact.model_dump(mode="json")


# ----------------------------------------------------------------- T06 历史运行

@router.get("/runs")
def list_runs(session_id: str) -> dict:
    store = SessionStore()
    try:
        return {"runs": store.list_runs(session_id)}
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/dashboard")
def get_run_dashboard(session_id: str, run_id: str) -> dict:
    """历史运行仪表盘：审计只读，不影响 active/current 指针。"""
    store = SessionStore()
    try:
        return store.read_dashboard(session_id, run_id=run_id)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/views/{view_id}/rows")
def get_view_rows(
    session_id: str,
    run_id: str,
    view_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> dict:
    """data_ref 明细分页：浏览器只拿当前页，全量明细留在服务器。"""
    store = SessionStore()
    try:
        df = store.read_run_view_result(session_id, run_id, view_id)
        ordered = ordered_detail(df)  # 离群行优先（无 is_outlier 列时原样返回）
        rows, meta = slice_page(ordered, page=page, page_size=page_size)
        envelope = ViewDataEnvelope(
            kind="detail",
            columns=build_columns(df),
            rows=rows,
            page=meta,
            data_ref=f"/sessions/{session_id}/runs/{run_id}/views/{view_id}/rows",
        )
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return envelope.model_dump(mode="json")
