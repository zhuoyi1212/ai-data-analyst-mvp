"""深度报告生成器（Task 6）。

LLM 基于“证据目录”（真实信号/链/视图数值）组织八节论证；输出经
report_validator 门禁（接地/证据/因果）。固件缺失或门禁两次不过时，
确定性模板兜底——模板只使用来自真实证据的数字，零编造。
"""
from __future__ import annotations

import json
import re
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.auto import EvidenceGraph, SignalSet
from app.schemas.bundle import AnalysisBundle, ViewType
from app.schemas.report import (
    AnalysisReportArtifact,
    REPORT_SECTION_IDS,
    ReportClaim,
    ReportSection,
    ReportSectionId,
)
from app.services.diagnostic_search import _now
from app.services.insight_validator import _extract_numbers
from app.services.llm import generate_json
from app.services.llm.errors import ContractError
from app.services.llm.fixtures import FixtureMissingError
from app.services.report_validator import validate_report
from app.services.storage import SessionStore

_SECTION_TITLES: dict[str, str] = {
    "exec_summary": "执行摘要",
    "performance": "核心表现",
    "drivers": "关键驱动因素",
    "risks": "风险与异常",
    "diagnostics": "深度诊断",
    "opportunities": "机会点",
    "recommendations": "结论与建议",
    "methodology": "证据与方法",
}

_STAGE = "report"

_SYSTEM = (
    "你是资深数据分析师，负责撰写结构化深度分析报告。规则：\n"
    "1. 只能使用证据目录中给出的数字，禁止心算、估算或生成目录以外的数字；\n"
    "2. 每条论断必须引用目录中真实存在的 view_id/probe_id；\n"
    "3. 禁止因果性词汇（导致/引起/使得/驱动/because/cause/drive/lead to），"
    "相关类内容用“相关/伴随/假设”表述并注明相关不等于因果；\n"
    "4. 八节都要输出；结论需呈现 结论→证据→拆解→线索→影响/风险/建议 的链条。\n"
    "仅输出 JSON。"
)


# ------------------------------------------------------------ LLM 契约


class _ClaimOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    claim_type: Any
    evidence_view_ids: list[str] = Field(min_length=1)
    limitations: str = ""


class _SectionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_id: ReportSectionId
    title: str = ""
    claims: list[_ClaimOut] = Field(default_factory=list)


class _ReportOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sections: list[_SectionOut]


# ------------------------------------------------------------ 证据目录


def _view_rows(
    store: SessionStore, sid: str, vid: str, *, limit: int = 6
) -> list[dict[str, Any]]:
    path = store.view_dir(sid, vid) / "result.parquet"
    if not path.exists():
        return []
    df = pd.read_parquet(path).head(limit)
    return json.loads(df.to_json(orient="records", force_ascii=False))


def _build_catalog(
    session_id: str,
    store: SessionStore,
    signal_set: SignalSet,
    bundle: AnalysisBundle,
) -> dict[str, Any]:
    views = []
    for v in bundle.analysis_views:
        views.append({
            "view_id": v.view_id,
            "type": getattr(v.type, "value", v.type),
            "metric_fields": v.metric_fields,
            "dimension_fields": v.dimension_fields,
            "rows": _view_rows(store, session_id, v.view_id),
        })
    chains = []
    if store.has_artifact(session_id, "chains"):
        payload = store.read_artifact(session_id, "chains")
        for c in payload["chains"]:
            chains.append({
                "chain_id": c["chain_id"],
                "root_signal_id": c["root_signal_id"],
                "root_question": c["root_question"],
                "conclusion": c["conclusion"],
                "evidence_refs": [
                    {"kind": n["kind"], "ref": n["ref_id"],
                     "dimension": n.get("dimension"), "depth": n["depth"]}
                    for n in c["nodes"] if n["kind"] != "root"
                ],
            })
    return {
        "signals": [
            {
                "signal_id": s.signal_id, "type": s.type, "metric": s.metric,
                "dimension": s.dimension, "member": s.member,
                "direction": s.direction,
                "magnitude": s.magnitude.model_dump(mode="json"),
                "scan_view_ids": s.scan_view_ids,
            }
            for s in signal_set.signals
        ],
        "chains": chains,
        "views": views,
    }


def _to_artifact(
    obj: _ReportOut,
    *,
    session_id: str,
    run_id: str,
    graph_ts: str,
) -> AnalysisReportArtifact:
    sections = [
        ReportSection(
            section_id=s.section_id,
            title=s.title or _SECTION_TITLES[s.section_id],
            claims=[
                ReportClaim(
                    claim_id=c.claim_id, text=c.text,
                    claim_type=c.claim_type,
                    evidence_view_ids=c.evidence_view_ids,
                    limitations=c.limitations,
                )
                for c in s.claims
            ],
        )
        for s in obj.sections
    ]
    return AnalysisReportArtifact(
        session_id=session_id, run_id=run_id, sections=sections,
        evidence_graph_created_at=graph_ts, generated_at=_now(),
    )


# ------------------------------------------------------------ 模板兜底


def _claim(
    cid: str, text: str, ctype: str, refs: list[str], *, limitations: str = ""
) -> ReportClaim:
    return ReportClaim(
        claim_id=cid, text=text, claim_type=ctype,  # type: ignore[arg-type]
        evidence_view_ids=refs, limitations=limitations,
    )


def _signal_text(s: Any) -> str:
    m = s.magnitude
    if s.type in ("decline", "growth"):
        word = "环比有所下滑" if s.type == "decline" else "环比有所改善"
        return (
            f"「{s.metric}」{word}：当前 {m.value:g}，"
            f"上期 {m.compare_value:g}。"
        )
    if s.type == "negative_member":
        return f"「{s.dimension}={s.member}」的「{s.metric}」为 {m.value:g}。"
    if s.type == "divergence":
        return (
            f"「{s.dimension}={s.member}」的「{s.metric}」"
            f"为 {m.value:g}，与其规模占比不匹配。"
        )
    if s.type == "anomaly":
        return f"「{s.metric}」检出离群值，需关注极端记录。"
    if s.type == "correlation":
        return (
            f"「{s.metric}」相关系数 {m.coefficient:g}"
            "（相关不代表因果，仅作线索）。"
        )
    if s.type == "simpson":
        return (
            f"「{s.metric}」整体率变化 {m.gap_pp:g} 个百分点，"
            "各组内部方向相反。"
        )
    if s.type == "scale_profit":
        return "出现增收不增利信号：规模上升而利润未同步改善。"
    return f"检测到信号：{s.type}。"


def _dedupe_signals(items: list) -> list:
    """按 (类型, 表述文本) 去重，保持优先级顺序（同一信号不在报告内重复）。"""
    seen: set[tuple] = set()
    out: list = []
    for s in items:
        key = (s.type, _signal_text(s))
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _dedup_claims(claims: list[ReportClaim]) -> list[ReportClaim]:
    """按文本去重（跨深挖链的重复证据只保留一条）并重排 claim_id。"""
    seen: set[str] = set()
    out: list[ReportClaim] = []
    for c in claims:
        if c.text in seen:
            continue
        seen.add(c.text)
        out.append(c)
    prefix = claims[0].claim_id.split("_")[0] if claims else "claim"
    return [
        c.model_copy(update={"claim_id": f"{prefix}_{i:02d}"})
        for i, c in enumerate(out, start=1)
    ]


def _probe_summaries(
    store: SessionStore, session_id: str, run_id: str, chain: dict,
) -> list[dict]:
    """从一条链的 group_by 探针结果中抽取「真实数值摘要」（最高/最低分组）。"""
    out: list[dict] = []
    for n in chain["nodes"]:
        if n["ref_type"] != "probe":
            continue
        pdir = (
            store.run_dir(session_id, run_id)
            / "diagnostic" / "probes" / n["ref_id"]
        )
        p = pdir / "result_group_by.parquet"
        if not p.exists():
            continue
        df = pd.read_parquet(p)
        if "value" not in df.columns or df.empty:
            continue
        dims = [c for c in df.columns if c != "value"]
        df = df.sort_values("value", ascending=False)
        top = df.iloc[0]
        bottom = df.iloc[-1]

        def label(row) -> str:
            return "、".join(f"{d}={row[d]}" for d in dims)

        top_label = label(top)
        bottom_label = label(bottom)
        # 单成员分组的探针不产出「最高 vs 最低」对比（避免零信息自比）
        if top_label == bottom_label:
            continue
        out.append({
            "probe_id": n["ref_id"],
            "dims": dims,
            "top_label": top_label,
            "top_value": round(float(top["value"]), 2),
            "bottom_label": bottom_label,
            "bottom_value": round(float(bottom["value"]), 2),
        })
    return out


def _template_report(
    session_id: str,
    run_id: str,
    signal_set: SignalSet,
    bundle: AnalysisBundle,
    store: SessionStore,
    graph_ts: str,
) -> AnalysisReportArtifact:
    signals = signal_set.signals
    # 兜底引用锚点：必须是结果文件真实存在的视图（否则门禁会判「无证据数据」）
    first_ref = next(
        (
            v.view_id for v in bundle.analysis_views
            if (
                store.view_dir(session_id, v.view_id) / "result.parquet"
            ).exists()
        ),
        bundle.analysis_views[0].view_id,
    )
    top = _dedupe_signals(signals)[:4]
    risk_types = {
        "decline", "negative_member", "divergence",
        "anomaly", "simpson", "scale_profit",
    }

    sections: list[ReportSection] = []

    # 1. exec_summary
    if top:
        claims = [
            _claim(f"sum_{i:02d}", _signal_text(s), "signal", s.scan_view_ids)
            for i, s in enumerate(top, start=1)
        ]
    else:
        claims = [_claim(
            "sum_01", "未检测到显著信号，整体表现平稳。", "conclusion",
            [first_ref],
        )]
    sections.append(ReportSection(
        section_id="exec_summary",
        title=_SECTION_TITLES["exec_summary"], claims=claims,
    ))

    # 2. performance：无维度的概览视图的真实度量值（执行失败/缺文件的视图跳过）
    perf_claims: list[ReportClaim] = []
    for v in bundle.analysis_views:
        if v.dimension_fields or not v.metric_fields:
            continue
        if v.type != ViewType.overview:
            continue
        ppath = store.view_dir(session_id, v.view_id) / "result.parquet"
        if not ppath.exists():
            continue
        df = pd.read_parquet(ppath)
        i = len(perf_claims)
        # aggregate 算子只输出 value 列；指标名取自视图的 metric_fields
        if "value" in df.columns:
            nums = pd.to_numeric(df["value"], errors="coerce").dropna()
            if not nums.empty:
                value = round(float(nums.iloc[0]), 2)
                label = v.metric_fields[0] if v.metric_fields else "核心指标"
                perf_claims.append(_claim(
                    f"perf_{i:02d}",
                    f"核心指标「{label}」当前为 {value:g}。", "fact",
                    [v.view_id],
                ))
                i += 1
        if len(perf_claims) >= 8:
            break
    if not perf_claims:
        perf_claims = [_claim(
            "perf_01", "当前数据无可用的总量概览指标。", "fact", [first_ref],
        )]
    sections.append(ReportSection(
        section_id="performance",
        title=_SECTION_TITLES["performance"], claims=perf_claims,
    ))

    # chains 证据 + 每条链的探针真实数值摘要
    chain_payload = None
    if store.has_artifact(session_id, "chains"):
        chain_payload = store.read_artifact(session_id, "chains")
    chain_list = chain_payload["chains"] if chain_payload else []
    chain_summaries = [
        (c, _probe_summaries(store, session_id, run_id, c))
        for c in chain_list
    ]

    # 3. drivers：拆解方向 + 探针量出的最高分组（数字接地）
    driver_claims: list[ReportClaim] = []
    for i, (c, summaries) in enumerate(chain_summaries, start=1):
        if not summaries:
            continue
        dims = sorted({d for s in summaries for d in s["dims"]})
        probe_refs = [s["probe_id"] for s in summaries]
        first = summaries[0]
        text = (
            f"沿 {'、'.join(dims)} 方向拆解，数值最高的分组为 "
            f"{first['top_label']}（{first['top_value']:g}），"
            f"最低为 {first['bottom_label']}（{first['bottom_value']:g}）。"
        )
        driver_claims.append(_claim(
            f"drv_{i:02d}", text, "conclusion", probe_refs,
        ))
    driver_claims = _dedup_claims(driver_claims) if driver_claims else []
    if not driver_claims:
        driver_claims = [_claim(
            "drv_01", "当前数据未形成维度驱动拆解。", "fact", [first_ref],
        )]
    sections.append(ReportSection(
        section_id="drivers",
        title=_SECTION_TITLES["drivers"], claims=driver_claims,
    ))

    # 4. risks：同表述去重；同一数字结论即使信号类型不同也不重复列出
    seen_nums: set[tuple] = set()
    risks: list = []
    for s in _dedupe_signals(
        [x for x in signals if x.type in risk_types]
    ):
        plains, pcts = _extract_numbers(_signal_text(s))
        key = tuple(plains + pcts)
        if key in seen_nums:
            continue
        seen_nums.add(key)
        risks.append(s)
    risk_claims = [
        _claim(f"risk_{i:02d}", _signal_text(s), "signal", s.scan_view_ids)
        for i, s in enumerate(risks[:6], start=1)
    ] or [_claim(
        "risk_01", "未发现显著风险信号。", "fact", [first_ref],
    )]
    sections.append(ReportSection(
        section_id="risks",
        title=_SECTION_TITLES["risks"], claims=risk_claims,
    ))

    # 5. diagnostics：各链最高/最低分组的真实量级差，作为待验证假设
    diag_claims: list[ReportClaim] = []
    for i, (_c, summaries) in enumerate(chain_summaries, start=1):
        for j, s in enumerate(summaries[:2], start=1):
            diag_claims.append(_claim(
                f"diag_{i:02d}_{j:02d}",
                f"假设异常与 {s['top_label']}（{s['top_value']:g}）和 "
                f"{s['bottom_label']}（{s['bottom_value']:g}）之间的量级差相关，"
                "需结合业务动作进一步验证。",
                "hypothesis", [s["probe_id"]],
                limitations="会计拆解与启发式排序，不构成因果结论。",
            ))
    if not diag_claims:
        diag_claims = [_claim(
            "diag_01", "当前数据未形成需要进一步验证的深度假设。", "fact",
            [first_ref],
        )]
    sections.append(ReportSection(
        section_id="diagnostics",
        title=_SECTION_TITLES["diagnostics"],
        claims=_dedup_claims(diag_claims) if diag_claims else [],
    ))

    # 6. opportunities
    growth = [s for s in signals if s.type == "growth"]
    opp_claims = [
        _claim(f"opp_{i:02d}", _signal_text(s), "signal", s.scan_view_ids)
        for i, s in enumerate(growth[:3], start=1)
    ] or [_claim(
        "opp_01", "当前数据中无明确的高置信机会信号，建议持续监测。", "fact",
        [first_ref],
    )]
    sections.append(ReportSection(
        section_id="opportunities",
        title=_SECTION_TITLES["opportunities"], claims=opp_claims,
    ))

    # 7. recommendations：锚定探针真实数值给出可执行建议
    rec_claims: list[ReportClaim] = []
    for i, (_c, summaries) in enumerate(chain_summaries, start=1):
        if not summaries:
            continue
        s = summaries[0]
        rec_claims.append(_claim(
            f"rec_{i:02d}",
            f"优先复核 {s['top_label']}（{s['top_value']:g}）与 "
            f"{s['bottom_label']}（{s['bottom_value']:g}）之间的差异，"
            "设计对照验证后再采取业务动作。",
            "conclusion", [s["probe_id"]],
        ))
    if not rec_claims:
        rec_claims = [_claim(
            "rec_01",
            "建议按深度诊断中的证据逐项复核，并设计对照验证后再采取业务动作。",
            "conclusion", [first_ref],
        )]
    sections.append(ReportSection(
        section_id="recommendations",
        title=_SECTION_TITLES["recommendations"],
        claims=_dedup_claims(rec_claims) if rec_claims else [],
    ))

    # 8. methodology
    method_claims = [_claim(
        "method_01",
        "本报告全部数字由确定性算子在分析视图上计算得出；LLM 仅负责语义理解、"
        "方向选择与论证组织，未生成任何数字；相关类结论仅为线索，不构成因果关系。",
        "fact", [first_ref],
    )]
    sections.append(ReportSection(
        section_id="methodology",
        title=_SECTION_TITLES["methodology"], claims=method_claims,
    ))

    return AnalysisReportArtifact(
        session_id=session_id, run_id=run_id, sections=sections,
        evidence_graph_created_at=graph_ts, generated_at=_now(),
    )


# ------------------------------------------------------------ 入口


def write_report(
    session_id: str, store: SessionStore
) -> AnalysisReportArtifact:
    graph = EvidenceGraph.model_validate(
        store.read_artifact(session_id, "evidence")
    )
    signal_set = SignalSet.model_validate(
        store.read_artifact(session_id, "signals")
    )
    bundle = AnalysisBundle.model_validate(
        store.read_bundle(session_id, graph.run_id)
    )
    catalog = _build_catalog(session_id, store, signal_set, bundle)
    valid_refs = {v["view_id"] for v in catalog["views"]}
    for c in catalog["chains"]:
        valid_refs.update(e["ref"] for e in c["evidence_refs"])

    report: AnalysisReportArtifact | None = None
    try:
        def _biz(obj: _ReportOut) -> list[str]:
            ids = [s.section_id for s in obj.sections]
            if sorted(ids) != sorted(REPORT_SECTION_IDS):
                return [f"必须且只能输出八节，当前：{sorted(ids)}"]
            bad_refs = {
                r for s in obj.sections for c in s.claims
                for r in c.evidence_view_ids if r not in valid_refs
            }
            if bad_refs:
                return [f"引用了目录之外的证据 id：{sorted(bad_refs)}"]
            artifact = _to_artifact(
                obj, session_id=session_id, run_id=graph.run_id,
                graph_ts=graph.created_at,
            )
            return validate_report(
                artifact, session_id=session_id, store=store, graph=graph,
            )

        result = generate_json(
            stage=_STAGE,
            fixture_name=f"report_{session_id[:8]}",
            system=_SYSTEM,
            user=json.dumps(
                {"evidence_catalog": catalog,
                 "section_titles": _SECTION_TITLES},
                ensure_ascii=False, default=str,
            ),
            schema=_ReportOut,
            max_repair=1,
            business_validator=_biz,
        )
        artifact = _to_artifact(
            result, session_id=session_id, run_id=graph.run_id,
            graph_ts=graph.created_at,
        )
        if not validate_report(
            artifact, session_id=session_id, store=store, graph=graph,
        ):
            report = artifact
    except (FixtureMissingError, ContractError):
        report = None

    if report is None:
        report = _template_report(
            session_id, graph.run_id, signal_set, bundle, store,
            graph.created_at,
        )

    store.write_artifact(
        session_id, "report", report.model_dump(mode="json")
    )
    return report
