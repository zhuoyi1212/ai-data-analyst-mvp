"""自然语言追问/下钻服务（Task 8）。

红线：
- 追问在当前 run 固化的 scope（全局筛选口径）内计算，不新开 run；
- 新视图**追加**进 bundle/execution/evidence graph/dashboard，原视图 parquet 字节不变；
- 答案数字全部来自新视图 parquet，接地校验不过即拒；
- LLM 只负责把问题翻译为既有算子方案；无 key 时走确定性规则方案。
"""
from __future__ import annotations

import contextlib
import logging
import os
import time
from typing import Any

import pandas as pd

from app.schemas.ask import AskArtifact, AskClaim, AskSet
from app.schemas.bundle import (
    AnalysisBundle,
    AnalysisView,
    BundleExecutionResult,
    ViewType,
)
from app.schemas.dictionary import DataDictionary
from app.schemas.plan import AnalysisPlan
from app.services.bundle_executor import _execute_one, _snapshot_hash
from app.services.bundle_planner import apply_scope_filters, normalize_scope
from app.services.dashboard_synthesizer import synthesize_dashboard
from app.services.diagnostic_search import _now
from app.services.engine.catalog import terminal_chart
from app.services.evidence_graph import build_evidence_graph
from app.services.executor import execute_plan
from app.services.insight_validator import _extract_numbers
from app.services.llm import generate_json
from app.services.llm.errors import ContractError
from app.services.llm.fixtures import FixtureMissingError
from app.services.offline_fallback import plan_for_question
from app.services.planner import _SYSTEM, _build_user, _business_errors
from app.services.report_validator import close_value, read_value_columns
from app.services.storage import SessionStore, StorageError

_OP_VIEW_TYPE: dict[str, ViewType] = {
    "aggregate": ViewType.overview,
    "time_series": ViewType.trend,
    "group_by": ViewType.comparison,
    "share": ViewType.breakdown,
    "top_n": ViewType.ranking,
    "correlation": ViewType.relationship,
    "outlier_flag": ViewType.anomaly,
    "derive_ratio": ViewType.profitability,
    "contribution": ViewType.contribution,
    "rate_decomposition": ViewType.rate_shift,
}


class AskError(Exception):
    """追问无法完成（方案不可执行/结果不可消费/答案未接地）。"""


def _make_plan(
    question: str,
    dictionary: DataDictionary,
    store: SessionStore,
    session_id: str,
    df: pd.DataFrame,
) -> AnalysisPlan:
    meta = store.get_meta(session_id)
    base = (meta.get("source_sample") or meta["filename"]).rsplit(".", 1)[0]
    names = [f"{base}_ask_plan", f"{base}_plan"]
    try:
        return generate_json(
            stage="plan", fixture_name=names, system=_SYSTEM,
            user=_build_user(dictionary, question), schema=AnalysisPlan,
            business_validator=lambda p: _business_errors(p, dictionary),
        )
    except FixtureMissingError:
        plan = plan_for_question(question, dictionary, store, session_id, df)
        errors = _business_errors(plan, dictionary)
        if errors:
            raise AskError("问题无法翻译为受支持的分析方案：" + "；".join(errors))
        return plan


def _terminal_fields(plan: AnalysisPlan) -> tuple[list[str], list[str]]:
    p = plan.steps[-1].params
    metric = getattr(p, "metric", None) or getattr(p, "column", None)
    numerator = getattr(p, "numerator", None)
    dim = getattr(p, "dimension", None)
    metrics = [x for x in (metric, numerator) if x]
    dims = [dim] if dim else []
    return metrics, dims


def _answer_claims(
    result: pd.DataFrame, view_id: str,
    *, store: SessionStore, session_id: str, run_id: str,
) -> list[AskClaim]:
    """从结果前三行构造接地答案；并逐数字校验在新视图中可溯源。"""
    num_cols = [c for c in result.columns if pd.api.types.is_numeric_dtype(result[c])]
    val_col = "value" if "value" in result.columns else (
        num_cols[0] if num_cols else None
    )
    key_cols = [c for c in result.columns if c != val_col]
    claims: list[AskClaim] = []
    for _, row in result.head(3).iterrows():
        if val_col is None or pd.isna(row[val_col]):
            continue
        value = float(row[val_col])
        if key_cols:
            key = "、".join(f"{c}={row[c]}" for c in key_cols)
            text = f"{key} 的指标值为 {value:g}。"
        else:
            text = f"计算结果为 {value:g}。"
        claims.append(AskClaim(text=text, evidence_view_ids=[view_id]))
    if not claims:
        raise AskError("追问结果无有效数值，无法形成答案。")

    columns = read_value_columns(
        store, session_id, run_id, view_id, "view"
    )
    pool: tuple[float, ...] = tuple(
        x for col_pool in columns.values() for x in col_pool
    )
    for claim in claims:
        plains, pcts = _extract_numbers(claim.text)
        for number in plains + pcts:
            # 同视图严格容差接地（大值允许相对误差，小值/比率收紧）
            if not any(close_value(number, x) for x in pool):
                raise AskError(f"答案数字未接地，已拒绝：{number:g}")
    return claims


@contextlib.contextmanager
def _ask_lock(session_id: str, store: SessionStore):
    """会话级追问互斥锁：串行化 read-modify-write，防止 view_id 撞号/文件撕裂。"""
    lock_path = store.session_dir(session_id) / ".ask.lock"
    fd = None
    for _ in range(50):  # 最多等待约 10s
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            time.sleep(0.2)
    if fd is None:
        raise AskError("已有追问正在处理中，请稍后再试。")
    try:
        yield
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _verify_fingerprints(session_id: str, store: SessionStore, manifest) -> None:
    """追问前实际重算 snapshot/dictionary 指纹，失配即拒（不旁路版本门禁）。"""
    actual_snapshot = _snapshot_hash(
        store.session_dir(session_id) / "snapshot.parquet"
    )
    if manifest.snapshot_hash != actual_snapshot:
        raise AskError(
            "数据快照已变更，当前分析运行已失效，请重新发起分析后再追问。"
        )
    dict_path = store.session_dir(session_id) / "dictionary.json"
    if manifest.dictionary_hash != store._sha256_file(dict_path):
        raise AskError(
            "数据字典已变更，当前分析运行已失效，请重新发起分析后再追问。"
        )


def ask(session_id: str, store: SessionStore, question: str) -> AskArtifact:
    manifest = store.run_manifest(session_id)
    run_id = manifest.run_id
    _verify_fingerprints(session_id, store, manifest)

    with _ask_lock(session_id, store):
        return _ask_locked(session_id, store, question, manifest, run_id)


def _ask_locked(
    session_id: str, store: SessionStore, question: str, manifest, run_id: str,
) -> AskArtifact:
    bundle_payload = store.read_bundle(session_id, run_id)
    dictionary = DataDictionary.model_validate(
        store.read_artifact(session_id, "dictionary")
    )
    snapshot = store.load_snapshot(session_id)
    scope_snapshot = apply_scope_filters(
        snapshot, normalize_scope(snapshot, manifest.scope)
    )

    plan = _make_plan(question, dictionary, store, session_id, scope_snapshot)
    op = plan.steps[-1].op
    try:
        result, _records, _elapsed = execute_plan(scope_snapshot, plan)
    except Exception as e:  # noqa: BLE001 —— 追问异常收敛为业务错误
        raise AskError(f"方案执行失败：{type(e).__name__}: {e}")
    if result is None or not len(result.index):
        raise AskError("追问结果为空，无法形成答案。")

    # 唯一新 view_id（追加，永不复用旧 id）
    existing = {v["view_id"] for v in bundle_payload["analysis_views"]}
    seq = len(bundle_payload["analysis_views"]) + 1
    while f"view_{seq:02d}" in existing:
        seq += 1
    view_id = f"view_{seq:02d}"

    metrics, dims = _terminal_fields(plan)
    view = AnalysisView(
        view_id=view_id, title=question[:40],
        type=_OP_VIEW_TYPE.get(op, ViewType.comparison),
        question=question, plan=plan, chart=terminal_chart(plan),
        priority=seq, metric_fields=metrics, dimension_fields=dims,
        source="ask", selection_reason="用户自然语言追问", role="presentation",
    )

    # 在当前 run 的 scope 快照上执行并落盘（写入 active run 的 views/ 目录）
    ver = _execute_one(
        session_id, store, view, scope_snapshot, len(scope_snapshot.index),
        manifest.snapshot_hash,
    )
    if not ver.consumable:
        raise AskError("追问视图未通过可消费校验，无法形成答案。")

    # 追加 bundle（run 内同文件，原视图字典原样保留）
    bundle = AnalysisBundle.model_validate(bundle_payload)
    bundle = bundle.model_copy(update={"analysis_views": [
        *bundle.analysis_views, view
    ]})
    bpath = store.run_dir(session_id, run_id) / "bundle.json"
    _atomic_json(bpath, bundle.model_dump(mode="json"))
    # bundle 文件已变 → 同步刷新 manifest.plan_hash，保持指纹可复检
    store._update_manifest(
        session_id, run_id, plan_hash=store._sha256_file(bpath)
    )

    # 追加 execution
    execution = BundleExecutionResult.model_validate(
        store.read_bundle_execution(session_id)
    )
    execution = execution.model_copy(update={
        "total": execution.total + 1,
        "succeeded": execution.succeeded + 1,
        "views": [*execution.views, ver],
    })
    store.write_bundle_execution(
        session_id, execution.model_dump(mode="json")
    )

    # 重建证据图谱（旧节点/边保留，追加新 view 节点）
    build_evidence_graph(session_id, store)

    # 同步重合成并原子重新发布（不删除、不悬空；GET 始终可用）
    dashboard_refreshed = True
    try:
        synthesize_dashboard(session_id, store, force=True)
    except Exception:  # noqa: BLE001 —— 不阻断追问，保留旧仪表盘并显式标记
        dashboard_refreshed = False
        logging.getLogger(__name__).warning(
            "追问后 Dashboard 重合成失败（session=%s run=%s new_view=%s），"
            "保留旧版仪表盘，bundle 与 dashboard 暂时不一致。",
            session_id, run_id, view_id,
        )

    claims = _answer_claims(
        result, view_id, store=store, session_id=session_id, run_id=run_id
    )

    artifact = AskArtifact(
        ask_id=f"ask_{view_id}",
        question=question, answer_claims=claims, new_view_ids=[view_id],
        scope_rows=len(scope_snapshot.index),
        # 报告八节为固定契约，追问不重写报告；问答本身即追加产物
        appended_report_section_ids=[],
        dashboard_refreshed=dashboard_refreshed,
        created_at=_now(),
    )

    try:
        ask_set = AskSet.model_validate(
            store.read_artifact(session_id, "asks")
        )
    except StorageError:
        ask_set = AskSet(session_id=session_id, run_id=run_id)
    ask_set = ask_set.model_copy(update={"asks": [*ask_set.asks, artifact]})
    store.write_artifact(session_id, "asks", ask_set.model_dump(mode="json"))
    return artifact


def _atomic_json(path, payload: Any) -> None:
    import json
    import os

    tmp = path.with_name(".bundle.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)
