"""Task 10：一键链路 E2E 验收 + 红线审计。

以 Superstore 量级数据离线走完整自治链路，断言产品验收数量指标：
- ≥8 个有价值（可消费）视图；
- ≥3 个有深度的关键结论（claim，且证据真实）；
- ≥1 条完成的自动深挖链；
- Dashboard 12-column 布局合法、state=ready；
- 报告通过 validator；全流程 ≤30s。

红线静态审计：
- engine 目录不导入任何 LLM 模块；
- 新增自治服务无 eval()/exec()/自由代码路径。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from app.schemas.auto import ChainSet
from app.services.evidence_graph import build_evidence_graph
from app.services.report_validator import validate_report

sys.path.insert(0, str(Path(__file__).parent))
from test_report import _report_pipeline  # noqa: E402
from test_signals import _superstore_df, _run_scan_and_signals  # noqa: E402
from app.services.signals import build_signal_set  # noqa: E402
from app.services.chain_diagnostic import run_chain_diagnostic  # noqa: E402
from app.services.report_writer import write_report  # noqa: E402
from app.services.dashboard_synthesizer import synthesize_dashboard  # noqa: E402


def _full_pipeline(tmp_path: Path):
    t0 = time.perf_counter()
    store, sid, bundle, execution, signals = _run_scan_and_signals(
        tmp_path, _superstore_df()
    )
    run_id = store.active_run_id(sid)
    store.write_artifact(
        sid, "signals",
        build_signal_set(sid, run_id, signals).model_dump(mode="json"),
    )
    chain_set = run_chain_diagnostic(sid, store)
    graph = build_evidence_graph(sid, store)
    dashboard = synthesize_dashboard(sid, store)
    report = write_report(sid, store)
    elapsed = time.perf_counter() - t0
    return store, sid, run_id, chain_set, graph, dashboard, report, elapsed


# ------------------------------------------------------------ TR-10.1


def test_one_click_pipeline_acceptance(tmp_path):
    store, sid, run_id, chain_set, graph, dashboard, report, elapsed = (
        _full_pipeline(tmp_path)
    )

    # ① ≥8 个有价值视图（可消费的 presentation 视图）
    valuable = [
        c for c in dashboard.views.values()
        if c.ref_type == "view" and c.consumable and c.role == "presentation"
    ]
    assert len(valuable) >= 8, f"仅有 {len(valuable)} 个价值视图"

    # ② ≥3 个有深度的关键结论（非纯 fact，或全部 claims 计；每条证据真实）
    claims = [c for s in report.sections for c in s.claims]
    deep = [c for c in claims if c.claim_type in ("conclusion", "signal")]
    assert len(deep) >= 3, f"仅有 {len(deep)} 个深度结论"
    for c in claims:
        for ref in c.evidence_view_ids:
            assert ref in dashboard.views

    # ③ ≥1 条完成的自动深挖链
    completed_chains = [c for c in chain_set.chains if c.status == "completed"]
    assert 1 <= len(completed_chains) <= 3
    # 深挖链确实产出探针证据
    assert any(c.diagnostic_views for c in completed_chains)

    # ④ Dashboard 状态与布局
    assert dashboard.state == "ready"
    assert dashboard.layout is not None
    used = 0
    for item in dashboard.layout.items:
        if used + item.col_span > 12:
            used = 0
        used += item.col_span
        assert used <= 12

    # ⑤ 报告全部接地、无违规
    assert validate_report(
        report, session_id=sid, store=store, graph=graph
    ) == []

    # ⑥ KPI 真实产出（非伪造）
    assert len(dashboard.kpis) >= 2

    # 性能（TR-10.3）：Superstore 量级全流程 ≤30s
    assert elapsed <= 30, f"全流程耗时 {elapsed:.1f}s"


# ------------------------------------------------------------ TR-10.2 红线


def test_engine_has_no_llm_imports():
    engine_dir = Path(__file__).resolve().parents[1] / "app" / "services" / "engine"
    files = list(engine_dir.rglob("*.py"))
    assert files
    for f in files:
        text = f.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith(("from app.services.llm", "import app.services.llm")):
                raise AssertionError(f"确定性引擎导入了 LLM：{f}")


def test_autonomous_services_no_dynamic_code():
    services = Path(__file__).resolve().parents[1] / "app" / "services"
    targets = [
        services / name for name in (
            "auto_analyst.py", "broad_scan.py", "signals.py",
            "chain_diagnostic.py", "evidence_graph.py",
            "report_writer.py", "report_validator.py",
            "layout_composer.py", "ask_service.py",
        )
    ]
    for f in targets:
        assert f.exists(), f
        text = f.read_text(encoding="utf-8")
        # 允许出现在注释/字符串说明中，但禁止真实调用 eval(/exec(
        for token in ("eval(", "exec("):
            assert token not in text, f"{f.name} 出现动态执行调用 {token}"
