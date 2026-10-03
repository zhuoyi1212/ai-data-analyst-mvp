"""一键自动分析编排器（Autonomous Analyst）。

对外入口：
    start_auto_analysis(session_id, store, answers=None) -> AutoAnalysisState
    get_auto_state(session_id, store) -> AutoAnalysisState

Task 1 阶段行为（后续任务替换中间阶段）：
    profile    自动生成/接受语义档案；无可用指标且存在未决字段时提问；
    quality    质量检测 + 保守默认决策（safe_demo_decisions：只转格式、其余保留）；
    scan/signals/diagnostic 本任务标记 skipped；
    synthesis  build_bundle → execute_bundle → synthesize_dashboard（旧版五段布局）。

红线：编排器只调用既有服务，不新造数字；任何异常收敛到 state 并落盘。
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.schemas.auto import (
    AUTO_STAGES,
    AutoAnalysisState,
    AutoStageName,
    GateQuestion,
    SignalSet,
    StageLog,
)
from app.schemas.dashboard import DashboardArtifact
from app.schemas.dictionary import DataDictionary
from app.schemas.quality import QualityReport
from app.services.broad_scan import plan_scan
from app.services.bundle_executor import execute_bundle
from app.services.chain_diagnostic import run_chain_diagnostic
from app.services.dashboard_synthesizer import synthesize_dashboard
from app.services.evidence_graph import build_evidence_graph
from app.services.signals import build_signal_set, extract_signals
from app.services.report_writer import write_report
from app.services.profiler import confirm_fields, generate_dictionary
from app.services.quality_actions import apply_decisions, safe_demo_decisions
from app.services.quality_checker import run_quality_checks
from app.services.storage import SessionStore, StorageError


# ---------------------------------------------------------------- 例外

class AutoGateNeed(Exception):
    """需要用户作答才能继续；携带最小化问题集。"""

    def __init__(self, questions: list[GateQuestion]):
        self.questions = questions
        super().__init__("needs_input")


class AutoFatal(Exception):
    """无法通过用户作答解除的严重问题（如零有效行）。"""


# ---------------------------------------------------------------- 工具

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _persist(store: SessionStore, state: AutoAnalysisState) -> None:
    state.updated_at = _now()
    store.write_artifact(
        state.session_id, "auto", state.model_dump(mode="json")
    )


def get_auto_state(session_id: str, store: SessionStore) -> AutoAnalysisState:
    return AutoAnalysisState.model_validate(
        store.read_artifact(session_id, "auto")
    )


# ---------------------------------------------------------------- 阶段：语义

def _unresolved_fields(dictionary: DataDictionary) -> list:
    """尚未确定：未忽略、未由用户确认、且（非高置信 或 语义 unknown）。"""
    out = []
    for f in dictionary.fields:
        if f.ignored or f.confirmed_by_user:
            continue
        if f.semantic_type.value == "unknown" or not f.confirmed:
            out.append(f)
    return out


def _metric_fields(dictionary: DataDictionary) -> list:
    return [
        f for f in dictionary.fields
        if not f.ignored and f.semantic_type.value == "metric"
    ]


def _stage_profile(session_id: str, store: SessionStore) -> DataDictionary:
    if not store.has_artifact(session_id, "dictionary"):
        generate_dictionary(session_id, store)
    dictionary = DataDictionary.model_validate(
        store.read_artifact(session_id, "dictionary")
    )

    if not dictionary.complete:
        unresolved = _unresolved_fields(dictionary)
        unknowns = [
            f for f in unresolved if f.semantic_type.value == "unknown"
        ]
        if not _metric_fields(dictionary):
            # 无可用指标：请用户对未决字段归类（可作答解除）
            questions = [
                GateQuestion(
                    question_id=f"profile:{f.name}",
                    stage="profile",
                    prompt=f"字段「{f.name}」的业务含义是什么？",
                    options=["metric", "dimension", "date", "ignore"],
                    allow_free_text=True,
                    context={"field": f.name},
                )
                for f in (unresolved or unknowns)
            ]
            raise AutoGateNeed(questions)
        # 有可用指标：自动接受其余字段的推荐语义，unknown 字段安全忽略
        decisions = [
            {"name": f.name, "semantic_type": f.semantic_type.value}
            for f in unresolved
            if f.semantic_type.value != "unknown"
        ]
        decisions += [
            {"name": f.name, "ignored": True} for f in unknowns
        ]
        if decisions:
            confirm_fields(session_id, decisions, store)
        dictionary = DataDictionary.model_validate(
            store.read_artifact(session_id, "dictionary")
        )

    if not _metric_fields(dictionary):
        raise AutoFatal("数据中没有可分析的数值指标，无法自动生成分析。")
    return dictionary


# ---------------------------------------------------------------- 阶段：质量

def _snapshot_clean(session_id: str, store: SessionStore, report: QualityReport) -> QualityReport:
    """零质量问题：快照即原始数据（不经过 apply_decisions，其要求至少一个 issue）。"""
    df = store.load_original(session_id)
    snapshot_hash = store.save_snapshot(session_id, df)
    report.complete = True
    report.snapshot_rows = int(df.shape[0])
    report.snapshot_hash = snapshot_hash
    store.write_artifact(
        session_id, "quality", report.model_dump(mode="json")
    )
    store.update_meta(
        session_id, stage="quality", snapshot_hash=snapshot_hash
    )
    return report


def _stage_quality(session_id: str, store: SessionStore) -> QualityReport:
    if not store.has_artifact(session_id, "quality"):
        run_quality_checks(session_id, store)
    report = QualityReport.model_validate(
        store.read_artifact(session_id, "quality")
    )
    if report.complete:
        pass  # 已有决策快照（如旧工作台处理过），直接复用
    elif not report.issues:
        report = _snapshot_clean(session_id, store, report)
    else:
        # 无人值守安全路径：只转格式、其余保留，不改变经营事实
        apply_decisions(session_id, safe_demo_decisions(report), store)
        report = QualityReport.model_validate(
            store.read_artifact(session_id, "quality")
        )
    snapshot = store.load_snapshot(session_id)
    if len(snapshot.index) == 0:
        raise AutoFatal("处理后的数据快照没有任何有效行，无法继续分析。")
    return report


# ---------------------------------------------------------------- 阶段：广扫


def _stage_scan(
    session_id: str, store: SessionStore, dictionary: DataDictionary
):
    """Broad Scan：规划八类视图并经既有门禁批量执行，返回 (bundle, run_id)。"""
    meta = store.get_meta(session_id)
    snapshot = store.load_snapshot(session_id)
    bundle = plan_scan(
        dictionary,
        snapshot,
        title=meta.get("filename", session_id),
    )
    store.write_bundle(session_id, bundle.model_dump(mode="json"))
    execute_bundle(session_id, store, bundle)
    run_id = store.active_run_id(session_id) or ""
    return bundle, run_id


# ---------------------------------------------------------------- 阶段：信号


def _stage_signals(session_id: str, store: SessionStore, state: AutoAnalysisState):
    """从 scan 结果提取信号、排序、落盘 SignalSet（top K 供 Diagnostic）。"""
    from app.schemas.bundle import AnalysisBundle, BundleExecutionResult

    run_id = store.active_run_id(session_id)
    if not run_id:
        raise AutoFatal("尚未完成广度扫描，无法进行信号检测。")
    bundle = AnalysisBundle.model_validate(store.read_bundle(session_id))
    execution = BundleExecutionResult.model_validate(
        store.read_bundle_execution(session_id)
    )
    dictionary = DataDictionary.model_validate(
        store.read_artifact(session_id, "dictionary")
    )
    signals = extract_signals(session_id, store, bundle, execution, dictionary)
    signal_set = build_signal_set(session_id, run_id, signals)
    store.write_artifact(
        session_id, "signals", signal_set.model_dump(mode="json")
    )
    state.artifacts["signals"] = run_id
    return signal_set


# ---------------------------------------------------------------- 阶段：综合


def _stage_synthesis(
    session_id: str, store: SessionStore
) -> DashboardArtifact:
    """证据图谱 → Dashboard → 深度报告（Task 7 换布局）。"""
    build_evidence_graph(session_id, store)
    artifact = synthesize_dashboard(session_id, store)
    write_report(session_id, store)
    return artifact


# ---------------------------------------------------------------- 作答处理

def _apply_answers(
    session_id: str, store: SessionStore,
    state: AutoAnalysisState, answers: dict[str, str],
) -> None:
    valid_ids = {q.question_id for q in state.needs_input}
    unknown = set(answers) - valid_ids
    if unknown:
        raise ValueError(f"存在当前未提出的问题 ID：{sorted(unknown)}")
    state.answers.update({k: str(v) for k, v in answers.items() if v != ""})

    # profile 阶段的作答 → 写回字典
    decisions: list[dict] = []
    for qid, answer in answers.items():
        if not qid.startswith("profile:"):
            continue
        field = qid.split(":", 1)[1]
        if answer == "ignore":
            decisions.append({"name": field, "ignored": True})
        elif answer in ("metric", "dimension", "date", "id", "geo"):
            decisions.append({"name": field, "semantic_type": answer})
        else:
            # 自由文本：作为业务含义说明；数值型默认按指标、否则按维度
            f = next(
                x for x in DataDictionary.model_validate(
                    store.read_artifact(session_id, "dictionary")
                ).fields if x.name == field
            )
            semantic = (
                "metric" if f.physical_type in {"integer", "float"} else "dimension"
            )
            decisions.append({
                "name": field, "semantic_type": semantic, "meaning": answer,
            })
    if decisions:
        confirm_fields(session_id, decisions, store)

    state.needs_input = []
    state.status = "running"
    _persist(store, state)


# ---------------------------------------------------------------- 主编排

def _init_state(session_id: str) -> AutoAnalysisState:
    return AutoAnalysisState(
        session_id=session_id,
        status="running",
        current_stage="profile",
        stages=[StageLog(stage=s) for s in AUTO_STAGES],
        created_at=_now(),
        updated_at=_now(),
    )


def _stage_index(stage: AutoStageName) -> int:
    return AUTO_STAGES.index(stage)


def start_auto_analysis(
    session_id: str,
    store: SessionStore,
    answers: dict[str, str] | None = None,
) -> AutoAnalysisState:
    """新建或续跑自动分析；幂等：已完成直接读回。"""
    if store.has_artifact(session_id, "auto"):
        state = get_auto_state(session_id, store)
    else:
        state = _init_state(session_id)
        _persist(store, state)

    if state.status == "completed":
        return state

    if answers:
        _apply_answers(session_id, store, state, answers)
    elif state.status == "waiting_input":
        return state

    dictionary: DataDictionary | None = None
    try:
        start_idx = _stage_index(state.current_stage)
        for idx in range(start_idx, len(AUTO_STAGES)):
            stage_name: AutoStageName = AUTO_STAGES[idx]  # type: ignore[assignment]
            state.current_stage = stage_name
            log = state.stages[idx]

            if stage_name == "diagnostic":
                # 无 top 信号 → 跳过；有信号 → 多 root 深挖
                signal_set = SignalSet.model_validate(
                    store.read_artifact(session_id, "signals")
                )
                if not signal_set.top_signal_ids:
                    log.status = "skipped"
                    log.message = "未检测到值得深挖的信号"
                    _persist(store, state)
                    continue
                log.status = "running"
                log.started_at = _now()
                _persist(store, state)
                chain_set = run_chain_diagnostic(session_id, store)
                state.artifacts["chains"] = chain_set.run_id
                log.message = (
                    f"自动深挖完成：{len(chain_set.chains)} 条分析链，"
                    f"{chain_set.budget['used_probes']} 个探针"
                )
                log.status = "completed"
                log.ended_at = _now()
                _persist(store, state)
                continue

            log.status = "running"
            log.started_at = _now()
            _persist(store, state)

            if stage_name == "profile":
                dictionary = _stage_profile(session_id, store)
                log.message = "数据语义已确认"
            elif stage_name == "quality":
                _stage_quality(session_id, store)
                log.message = "质量检测完成，已生成数据快照"
            elif stage_name == "scan":
                if dictionary is None:
                    dictionary = DataDictionary.model_validate(
                        store.read_artifact(session_id, "dictionary")
                    )
                _, run_id = _stage_scan(session_id, store, dictionary)
                state.run_id = run_id
                log.message = "广度扫描完成，多维视图已执行"
            elif stage_name == "signals":
                _stage_signals(session_id, store, state)
                log.message = "信号检测与排序完成"
            elif stage_name == "synthesis":
                artifact = _stage_synthesis(session_id, store)
                state.run_id = artifact.run_id
                log.message = "Dashboard 已生成"

            log.status = "completed"
            log.ended_at = _now()
            _persist(store, state)

        state.status = "completed"
        state.current_stage = "synthesis"
        state.artifacts["dashboard"] = state.run_id
        state.artifacts["report"] = state.run_id
        _persist(store, state)
    except AutoGateNeed as gate:
        log = state.stages[_stage_index(state.current_stage)]
        log.status = "blocked"
        log.ended_at = _now()
        state.status = "waiting_input"
        state.needs_input = gate.questions
        _persist(store, state)
    except AutoFatal as fatal:
        log = state.stages[_stage_index(state.current_stage)]
        log.status = "blocked"
        log.ended_at = _now()
        state.status = "failed"
        state.error = str(fatal)
        _persist(store, state)
    except Exception as exc:  # noqa: BLE001 —— 编排异常收敛为 failed
        log = state.stages[_stage_index(state.current_stage)]
        log.status = "blocked"
        log.ended_at = _now()
        state.status = "failed"
        state.error = f"{type(exc).__name__}: {exc}"
        _persist(store, state)
        raise
    return state
