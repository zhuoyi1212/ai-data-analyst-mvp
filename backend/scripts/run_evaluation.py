"""T12 产品价值评测 harness：静态 Bundle vs 受控 Loop。

对案例清单（scripts/evaluation/cases.py）中的每个带真值案例，用两份相互
独立的会话跑两条链路（输入 CSV 字节完全相同）：

- static（静态 Bundle）：
    上传 → 语义确认 → 质量处理 → build_bundle → execute_bundle
    → synthesize_dashboard
- loop（受控 Diagnostic Search Loop）：
    上述全部 + start_diagnostic_search（≤4 轮 / ≤8 probes / ≤24 终端执行）
    → 再合成 Dashboard

按《Trae执行清单》T12 汇总六项指标：
1. 定位/检出正确率（带真值案例；stress 案例不计入）
2. 误报率（no_problem 案例产出告警的比例）
3. 拒绝无效结论率（阴性案例 Loop 是否 no_signal 干净停止、0 下钻分支）
4. 证据接地完整率（Finding 的 evidence_view_ids 真实存在于视图字典的比例）
5. 平均 probe 数 / 轮数 / 终端执行数（Loop 预算审计）
6. 耗时（各阶段 wall time）

全程离线规则路径（LLM_FIXTURE_MODE=replay，无 key 自动规则降级），
不修改任何产品代码。

用法（backend/ 目录下）：
    uv run python scripts/run_evaluation.py                 # JSON 打到 stdout
    uv run python scripts/run_evaluation.py --out report.json
    uv run python scripts/run_evaluation.py --cases known_issue_region_collapse,no_problem_uniform
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# 强制离线评测：缺 LLM 配置时五个 AI 阶段一律规则降级，绝不触发外网调用。
os.environ.setdefault("LLM_FIXTURE_MODE", "replay")

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.schemas.bundle import ViewType  # noqa: E402
from app.schemas.dashboard import DashboardArtifact  # noqa: E402
from app.schemas.diagnostic import DiagnosticSearch  # noqa: E402
from app.schemas.dictionary import DataDictionary  # noqa: E402
from app.services.bundle_executor import execute_bundle  # noqa: E402
from app.services.bundle_planner import build_bundle  # noqa: E402
from app.services.dashboard_synthesizer import synthesize_dashboard  # noqa: E402
from app.services.diagnostic_search import start_diagnostic_search  # noqa: E402
from app.services.profiler import confirm_fields, generate_dictionary  # noqa: E402
from app.services.quality_actions import (  # noqa: E402
    apply_decisions,
    safe_demo_decisions,
)
from app.services.quality_checker import run_quality_checks  # noqa: E402
from app.services.storage import SessionStore  # noqa: E402
from scripts.evaluation.cases import CASES, CASES_BY_ID, EvalCase  # noqa: E402

# T09 预算硬边界（与 DiagnosticSearch 默认值一致，审计用）
CAPS = {"max_rounds": 4, "max_probes": 8, "max_terminal_executions": 24, "max_depth": 3}


# ================================================================ 链路准备


def _prepare(case: EvalCase, root: Path) -> dict:
    """上传→语义→质量→Bundle→执行，返回 store/sid 与各阶段耗时。"""
    phases: dict[str, float] = {}

    t0 = time.perf_counter()
    store = SessionStore(root)
    meta = store.create_from_bytes(f"{case.case_id}.csv", case.builder())
    sid = meta["session_id"]
    dictionary = generate_dictionary(sid, store)
    pending = [
        {"name": f.name,
         "semantic_type": case.semantic_overrides.get(f.name, f.semantic_type.value)}
        for f in dictionary.fields if not f.confirmed and not f.ignored
    ]
    if pending:
        confirm_fields(sid, pending, store)
    report = run_quality_checks(sid, store)
    if case.quality_mode == "safe":
        # 仅转格式：离群/缺失/重复保留（模拟用户未确认剔除，防止评测走理想路径）
        decisions = safe_demo_decisions(report)
    else:
        decisions = {i.issue_id: {"action": i.suggested_action} for i in report.issues}
    apply_decisions(sid, decisions, store)
    phases["prep_s"] = time.perf_counter() - t0

    snapshot = store.load_snapshot(sid)
    dictionary = DataDictionary.model_validate(store.read_artifact(sid, "dictionary"))

    t0 = time.perf_counter()
    bundle = build_bundle(
        dictionary, snapshot, snapshot_rows=len(snapshot), title=case.case_id
    )
    store.write_bundle(sid, bundle.model_dump(mode="json"))
    execution = execute_bundle(sid, store, bundle)
    phases["bundle_s"] = time.perf_counter() - t0

    return {
        "store": store, "sid": sid,
        "bundle_total": execution.total,
        "bundle_succeeded": execution.succeeded,
        "bundle_failed": execution.failed,
        "phases": phases,
    }


# ================================================================ 评分


def _finding_titles(artifact: DashboardArtifact) -> list[str]:
    return [f.title for f in artifact.findings]


def _member_text_hits(artifact: DashboardArtifact, member: str) -> list[str]:
    """静态 Bundle 无下钻树：检查 Finding/View 文案是否点名问题成员。"""
    hits: list[str] = []
    for f in artifact.findings:
        if member in f.title or member in f.summary:
            hits.append(f"finding:{f.finding_id}")
    for vid, v in artifact.views.items():
        for text in (v.title, v.question, v.interpretation):
            if member in text:
                hits.append(f"view:{vid}")
                break
    return hits


def _branch_hit(search: DiagnosticSearch, dimension: str, member: str) -> bool:
    """Loop 定位正确：存在下钻子节点锚定 dimension=member。"""
    for node in search.nodes:
        if node.parent_id is None or node.dimension != dimension:
            continue
        if any(
            sf.column == dimension and member in sf.values
            for sf in node.scope_filters
        ):
            return True
    return False


def _keyword_hit(artifact: DashboardArtifact, keywords: tuple[str, ...]) -> str | None:
    for title in _finding_titles(artifact):
        for kw in keywords:
            if kw in title:
                return kw
    return None


def _relationship_claims(artifact: DashboardArtifact) -> list[str]:
    """引用 relationship 视图的 Finding（正负相关的关系结论都算）。"""
    rel_views = {
        vid for vid, v in artifact.views.items()
        if v.type is ViewType.relationship
    }
    return [
        f.finding_id for f in artifact.findings
        if any(ref in rel_views for ref in f.evidence_view_ids)
    ]


def _consumable_relationship_views(artifact: DashboardArtifact) -> list[str]:
    return [
        vid for vid, v in artifact.views.items()
        if v.type is ViewType.relationship and v.consumable
    ]


def _grounding(artifact: DashboardArtifact) -> tuple[int, int]:
    total = grounded = 0
    for f in artifact.findings:
        for ref in f.evidence_view_ids:
            total += 1
            if ref in artifact.views:
                grounded += 1
    return grounded, total


def _budget_violations(search: DiagnosticSearch) -> list[str]:
    b = search.budget
    violations: list[str] = []
    if b.used_rounds > CAPS["max_rounds"]:
        violations.append(f"rounds={b.used_rounds}>{CAPS['max_rounds']}")
    if b.used_probes > CAPS["max_probes"]:
        violations.append(f"probes={b.used_probes}>{CAPS['max_probes']}")
    if b.used_terminal_executions > CAPS["max_terminal_executions"]:
        violations.append(
            f"executions={b.used_terminal_executions}>{CAPS['max_terminal_executions']}")
    depth = max((n.depth for n in search.nodes), default=0)
    if depth > CAPS["max_depth"]:
        violations.append(f"depth={depth}>{CAPS['max_depth']}")
    if b.used_terminal_executions > 3 * b.used_probes:
        violations.append(
            f"executions={b.used_terminal_executions}>3*probes={3 * b.used_probes}")
    return violations


def _truth_hit(case: EvalCase, artifact: DashboardArtifact,
               search: DiagnosticSearch | None) -> tuple[bool, str]:
    """返回 (是否命中, 判定依据)。search=None 表示静态 Bundle 模式。"""
    if case.truth_kind == "finding_keyword":
        kw = _keyword_hit(artifact, case.expected_keywords)
        return (kw is not None, f"keyword:{kw}" if kw else "keyword_miss")
    if case.truth_kind == "relationship_absence":
        claims = _relationship_claims(artifact)
        cards = _consumable_relationship_views(artifact)
        ok = not claims and not cards
        return ok, (
            "no_relationship_claim" if ok
            else f"claims={claims},consumable_rel_views={cards}"
        )
    if case.truth_kind == "branch":
        assert case.expected_dimension and case.expected_member
        if search is not None:
            ok = _branch_hit(search, case.expected_dimension, case.expected_member)
            if ok:
                return True, f"branch:{case.expected_dimension}={case.expected_member}"
            return False, "branch_miss"
        hits = _member_text_hits(artifact, case.expected_member)
        return (bool(hits), hits[0] if hits else "member_not_named")
    return False, "no_truth_defined"


# ================================================================ 单案例两模式


def _evaluate_case(case: EvalCase, base_root: Path) -> dict:
    # ---- static
    static_dir = base_root / case.case_id / "static"
    pre = _prepare(case, static_dir)
    t0 = time.perf_counter()
    static_artifact = synthesize_dashboard(pre["sid"], pre["store"])
    pre["phases"]["synth_s"] = time.perf_counter() - t0
    s_hit, s_basis = _truth_hit(case, static_artifact, None)
    s_refs, s_grounded = _grounding(static_artifact)
    static_elapsed = sum(pre["phases"].values())

    # ---- loop（独立会话，相同输入）
    loop_dir = base_root / case.case_id / "loop"
    pre2 = _prepare(case, loop_dir)
    _ = synthesize_dashboard(pre2["sid"], pre2["store"])  # 与 static 同基线
    t0 = time.perf_counter()
    search = start_diagnostic_search(pre2["sid"], pre2["store"])
    pre2["phases"]["search_s"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    loop_artifact = synthesize_dashboard(pre2["sid"], pre2["store"])
    pre2["phases"]["resynth_s"] = time.perf_counter() - t0
    l_hit, l_basis = _truth_hit(case, loop_artifact, search)
    l_refs, l_grounded = _grounding(loop_artifact)
    loop_elapsed = sum(pre2["phases"].values())
    branches = [n for n in search.nodes if n.parent_id is not None]
    violations = _budget_violations(search)

    s_claims = _relationship_claims(static_artifact)
    l_claims = _relationship_claims(loop_artifact)
    stop_ok = (
        search.stop_reason in case.expected_stop_reasons
        if case.expected_stop_reasons else None
    )

    return {
        "case_id": case.case_id,
        "title": case.title,
        "case_type": case.case_type,
        "domain": case.domain,
        "expect_signal": case.expect_signal,
        "fp_policy": case.fp_policy,
        "expected_stop_reasons": list(case.expected_stop_reasons),
        "note": case.note,
        "static": {
            "state": static_artifact.state,
            "bundle": {"total": pre["bundle_total"], "succeeded": pre["bundle_succeeded"],
                       "failed": pre["bundle_failed"]},
            "findings": len(static_artifact.findings),
            "relationship_claims": len(s_claims),
            "consumable_relationship_views":
                len(_consumable_relationship_views(static_artifact)),
            "truth_hit": s_hit,
            "hit_basis": s_basis,
            "evidence_refs": s_refs,
            "evidence_grounded": s_grounded,
            "phases_s": {k: round(v, 4) for k, v in pre["phases"].items()},
            "elapsed_s": round(static_elapsed, 4),
        },
        "loop": {
            "search_state": search.state,
            "stop_reason": search.stop_reason,
            "stop_reason_ok": stop_ok,
            "rounds": search.budget.used_rounds,
            "probes": search.budget.used_probes,
            "terminal_executions": search.budget.used_terminal_executions,
            "expanded_branches": len(branches),
            "max_depth": max((n.depth for n in search.nodes), default=0),
            "budget_violations": violations,
            "findings": len(loop_artifact.findings),
            "relationship_claims": len(l_claims),
            "consumable_relationship_views":
                len(_consumable_relationship_views(loop_artifact)),
            "truth_hit": l_hit,
            "hit_basis": l_basis,
            "evidence_refs": l_refs,
            "evidence_grounded": l_grounded,
            "clean_rejection": (
                search.stop_reason == "no_signal" and len(branches) == 0
            ),
            "phases_s": {k: round(v, 4) for k, v in pre2["phases"].items()},
            "elapsed_s": round(loop_elapsed, 4),
        },
    }


# ================================================================ 汇总


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _is_false_positive(row: dict, mode: str, *, loop: bool) -> bool:
    """按案例声明的误报口径判定（relationship_only 时合法的异常告警不算误报）。"""
    m = row[mode]
    if row["fp_policy"] == "relationship_only":
        return m["relationship_claims"] > 0 or m["consumable_relationship_views"] > 0
    return m["findings"] > 0 or (loop and m["expanded_branches"] > 0)


def _correctly_rejected(row: dict, mode: str) -> bool:
    """Loop 阴性案例的「正确拒绝」：

    - relationship_only：门禁挡住相关结论即可（合法异常路径允许展开）；
    - any_finding：必须 no_signal 且 0 分支的干净停止。
    """
    m = row[mode]
    if row["fp_policy"] == "relationship_only":
        return m["relationship_claims"] == 0 \
            and m["consumable_relationship_views"] == 0
    return m["clean_rejection"]


def _aggregate(case_results: list[dict]) -> dict:
    truth = [r for r in case_results
             if r["expect_signal"] and r["case_type"] != "stress"]
    negatives = [r for r in case_results if not r["expect_signal"]]
    stop_checked = [
        r for r in case_results if r["expected_stop_reasons"]
    ]

    def mode_block(mode: str, *, loop: bool) -> dict:
        block = {
            "detection_rate": _rate(
                sum(1 for r in truth if r[mode]["truth_hit"]), len(truth)),
            "truth_cases": len(truth),
            "false_positive_rate": _rate(
                sum(1 for r in negatives
                    if _is_false_positive(r, mode, loop=loop)),
                len(negatives)),
            "negative_cases": len(negatives),
            "evidence_grounding_rate": _rate(
                sum(r[mode]["evidence_grounded"] for r in case_results),
                sum(r[mode]["evidence_refs"] for r in case_results)),
            "avg_elapsed_s": _mean(
                [r[mode]["elapsed_s"] for r in case_results]),
            "max_elapsed_s": round(
                max((r[mode]["elapsed_s"] for r in case_results), default=0), 4),
        }
        if loop:
            block.update({
                "invalid_conclusion_rejection_rate": _rate(
                    sum(1 for r in negatives if _correctly_rejected(r, mode)),
                    len(negatives)),
                "avg_probes": round(
                    sum(r[mode]["probes"] for r in case_results) / len(case_results), 2),
                "max_probes": max(r[mode]["probes"] for r in case_results),
                "avg_rounds": round(
                    sum(r[mode]["rounds"] for r in case_results) / len(case_results), 2),
                "avg_terminal_executions": round(
                    sum(r[mode]["terminal_executions"] for r in case_results)
                    / len(case_results), 2),
                "budget_violations": sum(
                    1 for r in case_results if r[mode]["budget_violations"]),
                "avg_search_elapsed_s": round(
                    sum(r[mode]["phases_s"].get("search_s", 0)
                        for r in case_results) / len(case_results), 4),
                "stop_reason_expectation_rate": _rate(
                    sum(1 for r in stop_checked
                        if r[mode]["stop_reason_ok"] is True),
                    len(stop_checked)),
                "stop_reason_checked_cases": len(stop_checked),
            })
        return block

    return {
        "total_cases": len(case_results),
        "static": mode_block("static", loop=False),
        "loop": mode_block("loop", loop=True),
    }


def run_evaluation(case_ids: list[str] | None = None,
                   store_root: Path | None = None) -> dict:
    if case_ids:
        unknown = [c for c in case_ids if c not in CASES_BY_ID]
        if unknown:
            raise ValueError(f"未知案例 id：{unknown}；可选：{sorted(CASES_BY_ID)}")
        selected = tuple(CASES_BY_ID[c] for c in case_ids)
    else:
        selected = CASES

    created = False
    if store_root is None:
        store_root = Path(tempfile.mkdtemp(prefix="aida_eval_"))
        created = True
    store_root.mkdir(parents=True, exist_ok=True)

    results = [_evaluate_case(case, store_root) for case in selected]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "offline_rule_based",
        "budget_caps": CAPS,
        "store_root": str(store_root),
        "store_root_auto_created": created,
        "totals": {
            "cases": len(results),
            "truth_cases": sum(
                1 for r in results if r["expect_signal"] and r["case_type"] != "stress"),
            "negative_cases": sum(1 for r in results if not r["expect_signal"]),
            "stress_cases": sum(1 for r in results if r["case_type"] == "stress"),
            "cases_by_type": dict(sorted({
                t: sum(1 for r in results if r["case_type"] == t)
                for r in results for t in [r["case_type"]]
            }.items())),
        },
        "metrics": _aggregate(results),
        "cases": results,
    }


# ================================================================ CLI


def _print_summary(report: dict) -> None:
    m = report["metrics"]
    line = lambda *xs: print(*xs, file=sys.stderr)
    line(f"评测完成：{report['totals']['cases']} 个案例（真值 "
         f"{report['totals']['truth_cases']} / 阴性 "
         f"{report['totals']['negative_cases']} / 压力 "
         f"{report['totals']['stress_cases']}），存储根目录：{report['store_root']}")
    for mode, label in (("static", "静态Bundle"), ("loop", "受控Loop ")):
        b = m[mode]
        line(
            f"  {label}：检出/定位正确率 {b['detection_rate']}，"
            f"误报率 {b['false_positive_rate']}，"
            f"证据接地 {b['evidence_grounding_rate']}，"
            f"平均耗时 {b['avg_elapsed_s']}s")
    lb = m["loop"]
    line(f"  Loop 预算：平均 probes={lb['avg_probes']}（max {lb['max_probes']}），"
         f"平均终端执行={lb['avg_terminal_executions']}，"
         f"预算违规 {lb['budget_violations']} 例，"
         f"无效结论拒绝率 {lb['invalid_conclusion_rejection_rate']}")
    for r in report["cases"]:
        line(f"    [{r['case_id']}] static_hit={r['static']['truth_hit']}"
             f"({r['static']['hit_basis']})  loop_hit={r['loop']['truth_hit']}"
             f"({r['loop']['hit_basis']})  stop={r['loop']['stop_reason']}"
             f"  probes={r['loop']['probes']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T12 产品价值评测 harness")
    parser.add_argument("--out", type=Path, default=None,
                        help="JSON 报告输出路径；缺省打到 stdout")
    parser.add_argument("--cases", type=str, default=None,
                        help="逗号分隔的案例 id；缺省跑全部案例")
    parser.add_argument("--store-root", type=Path, default=None,
                        help="会话存储根目录；缺省使用系统临时目录")
    args = parser.parse_args(argv)

    case_ids = [s.strip() for s in args.cases.split(",")] if args.cases else None
    report = run_evaluation(case_ids=case_ids, store_root=args.store_root)
    payload = json.dumps(report, ensure_ascii=False, indent=2)

    _print_summary(report)
    if args.out:
        args.out.write_text(payload, encoding="utf-8")
        print(f"JSON 报告已写入：{args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
