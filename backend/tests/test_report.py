"""Task 6：AnalysisReportArtifact 深度报告测试。

覆盖：
- TR-6.1 八节齐全、每 claim 证据存在、数字接地，validator 通过；
- TR-6.2 未接地数字/不存在 view_id/因果词三类脏报告必被拦截；
- TR-6.3 模板报告数字抽样可溯源到真实证据；GET /report 端点。
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.schemas.report import (
    AnalysisReportArtifact,
    ReportClaim,
    ReportSection,
)
from app.services.chain_diagnostic import run_chain_diagnostic
from app.services.evidence_graph import build_evidence_graph
from app.services.report_validator import (
    close_value,
    is_metric_col,
    read_string_tokens,
    read_value_columns,
    label_token_ok,
    validate_report,
)
from app.services.report_writer import write_report
from app.services.signals import build_signal_set
from app.services.insight_validator import _extract_numbers

sys.path.insert(0, str(Path(__file__).parent))
from test_signals import _superstore_df, _run_scan_and_signals  # noqa: E402


def _report_pipeline(tmp_path: Path):
    store, sid, bundle, execution, signals = _run_scan_and_signals(
        tmp_path, _superstore_df()
    )
    run_id = store.active_run_id(sid)
    store.write_artifact(
        sid, "signals",
        build_signal_set(sid, run_id, signals).model_dump(mode="json"),
    )
    run_chain_diagnostic(sid, store)
    graph = build_evidence_graph(sid, store)
    report = write_report(sid, store)
    return store, sid, run_id, graph, report


def _dirty(
    report: AnalysisReportArtifact, *, section: str, claim: ReportClaim
) -> AnalysisReportArtifact:
    sections = [
        s.model_copy(update={"claims": [claim]}) if s.section_id == section else s
        for s in report.sections
    ]
    return report.model_copy(update={"sections": sections})


# ------------------------------------------------------------ TR-6.1


def test_report_eight_sections_and_valid(tmp_path):
    store, sid, run_id, graph, report = _report_pipeline(tmp_path)

    assert len(report.sections) == 8
    violations = validate_report(
        report, session_id=sid, store=store, graph=graph
    )
    assert violations == []

    # 每 claim 至少一条证据
    for s in report.sections:
        for c in s.claims:
            assert c.evidence_view_ids


# ------------------------------------------------------------ TR-6.2


def test_dirty_ungrounded_number_blocked(tmp_path):
    store, sid, run_id, graph, report = _report_pipeline(tmp_path)
    first_ref = report.sections[0].claims[0].evidence_view_ids[0]
    bad = _dirty(report, section="exec_summary", claim=ReportClaim(
        claim_id="bad_01", text="神奇数字 99999.9 出现了。",
        claim_type="fact", evidence_view_ids=[first_ref],
    ))

    violations = validate_report(
        bad, session_id=sid, store=store, graph=graph
    )
    assert any("99999.9" in v and "未接地" in v for v in violations)


def test_dirty_missing_view_blocked(tmp_path):
    store, sid, run_id, graph, report = _report_pipeline(tmp_path)
    bad = _dirty(report, section="exec_summary", claim=ReportClaim(
        claim_id="bad_02", text="引用了一个假视图。",
        claim_type="fact", evidence_view_ids=["view_not_exist"],
    ))

    violations = validate_report(
        bad, session_id=sid, store=store, graph=graph
    )
    assert any("view_not_exist" in v and "不存在" in v for v in violations)


def test_dirty_causal_term_blocked(tmp_path):
    store, sid, run_id, graph, report = _report_pipeline(tmp_path)
    first_ref = report.sections[0].claims[0].evidence_view_ids[0]
    bad = _dirty(report, section="exec_summary", claim=ReportClaim(
        claim_id="bad_03", text="折扣导致了利润恶化。",
        claim_type="conclusion", evidence_view_ids=[first_ref],
    ))

    violations = validate_report(
        bad, session_id=sid, store=store, graph=graph
    )
    assert any("因果性表述" in v for v in violations)


# ------------------------------------------------------------ TR-6.3


def test_template_numbers_traceable_to_evidence(tmp_path):
    store, sid, run_id, graph, report = _report_pipeline(tmp_path)
    node_types = {n.node_id: n.type for n in graph.nodes}

    checked = 0
    for section in report.sections:
        for claim in section.claims:
            plains, pcts = _extract_numbers(claim.text)
            if not plains and not pcts:
                continue
            columns: dict = {}
            tokens: set = set()
            for ref in claim.evidence_view_ids:
                ref_columns = read_value_columns(
                    store, sid, run_id, ref, node_types[ref]
                )
                for col, pool in ref_columns.items():
                    columns[col] = columns.get(col, ()) + pool
                tokens |= read_string_tokens(
                    store, sid, run_id, ref, node_types[ref]
                )
            metric_pool = tuple(
                x for col, pool in columns.items() if is_metric_col(col)
                for x in pool
            )
            for value in plains + pcts:
                traceable = any(close_value(value, x) for x in metric_pool)
                if not traceable:
                    traceable = label_token_ok(value, claim.text, tokens)
                assert traceable, f"{value:g} 无法溯源"
                checked += 1
    assert checked >= 5  # 确有抽样


def test_report_endpoint(tmp_path, monkeypatch):
    import dataclasses

    from fastapi.testclient import TestClient

    import app.services.storage as storage_mod
    from app.config import settings as app_settings
    from app.main import app

    monkeypatch.setattr(
        storage_mod, "settings",
        dataclasses.replace(app_settings, storage_dir=tmp_path / "store"),
    )
    store, sid, run_id, graph, report = _report_pipeline(tmp_path)
    client = TestClient(app)

    resp = client.get(f"/sessions/{sid}/report")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["sections"]) == 8
