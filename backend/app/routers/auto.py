"""一键自动分析路由（Autonomous Analyst）。

POST /sessions/{id}/auto-analyze  启动/续跑（可选 body：{"answers": {...}}）
GET  /sessions/{id}/auto-analyze  查询当前阶段状态
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas.ask import AskRequest
from app.schemas.auto import AutoAnalysisState, AutoAnswerRequest, EvidenceGraph
from app.services.ask_service import AskError, ask
from app.services.auto_analyst import get_auto_state, start_auto_analysis
from app.services.evidence_graph import neighborhood
from app.services.storage import SessionStore, StorageError

router = APIRouter(prefix="/sessions/{session_id}", tags=["auto"])


@router.post("/auto-analyze")
def run_auto(
    session_id: str, request: AutoAnswerRequest | None = None
) -> dict:
    store = SessionStore()
    answers = request.answers if request is not None else None
    try:
        state = start_auto_analysis(session_id, store, answers=answers)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if state.status == "failed":
        raise HTTPException(status_code=422, detail=state.error)
    return state.model_dump(mode="json")


@router.get("/auto-analyze")
def read_auto(session_id: str) -> dict:
    try:
        state = get_auto_state(session_id, SessionStore())
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return state.model_dump(mode="json")


@router.get("/evidence")
def read_evidence(session_id: str, node: str | None = None) -> dict:
    store = SessionStore()
    try:
        if store.has_artifact(session_id, "evidence"):
            graph = EvidenceGraph.model_validate(
                store.read_artifact(session_id, "evidence")
            )
        else:
            from app.services.evidence_graph import build_evidence_graph
            graph = build_evidence_graph(session_id, store)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if node is not None:
        if not any(n.node_id == node for n in graph.nodes):
            raise HTTPException(status_code=404, detail=f"图中无此节点：{node}")
        graph = neighborhood(graph, node)
    return graph.model_dump(mode="json")


@router.get("/report")
def read_report(session_id: str) -> dict:
    store = SessionStore()
    try:
        if store.has_artifact(session_id, "report"):
            payload = store.read_artifact(session_id, "report")
        else:
            from app.services.report_writer import write_report
            payload = write_report(session_id, store).model_dump(mode="json")
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return payload


@router.post("/ask")
def run_ask(session_id: str, request: AskRequest) -> dict:
    try:
        artifact = ask(session_id, SessionStore(), request.question)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AskError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return artifact.model_dump(mode="json")
