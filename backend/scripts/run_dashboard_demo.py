"""重构 Phase 2 实测脚本：Superstore 端到端生成 DashboardArtifact 并打印摘要。

用法（backend 目录下）：
    PYTHONPATH=. uv run python scripts/run_dashboard_demo.py <文件路径>

在 run_bundle_demo 的 Bundle 管线之后，继续：
  合成 DashboardArtifact（KPI/五段布局/ChartSpec/全局筛选）→ 四类 Findings
  （含 drilldown probe），打印真实计算结果摘要。全程离线规则路径。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.schemas.bundle import ViewType, ViewStatus
from app.schemas.dashboard import DashboardArtifact
from app.services.bundle_executor import execute_bundle
from app.services.bundle_planner import build_bundle
from app.services.dashboard_synthesizer import synthesize_dashboard
from app.services.profiler import confirm_fields, generate_dictionary
from app.services.quality_actions import apply_decisions
from app.services.quality_checker import run_quality_checks
from app.services.storage import SessionStore


def _prepare(store: SessionStore, path: Path) -> tuple[str, int]:
    meta = store.create_from_bytes(path.name, path.read_bytes())
    sid = meta["session_id"]
    print(f"会话：{sid}")
    print(f"原始数据：{meta['filename']} · {meta['shape']['rows']} 行 × {meta['shape']['cols']} 列\n")

    import re
    dictionary = generate_dictionary(sid, store)
    id_hint = re.compile(r"(id|编码|编号|邮编|code)$", re.I)

    def auto(f):
        if f.semantic_type.value != "unknown":
            return f.semantic_type.value
        return "id" if id_hint.search(f.name) else "dimension"

    pending = [
        {"name": f.name, "semantic_type": auto(f)}
        for f in dictionary.fields if not f.confirmed and not f.ignored
    ]
    if pending:
        confirm_fields(sid, pending, store)

    # T02：演示路径只转格式，缺失/离群/重复默认保留，不伪装用户确认
    from app.services.quality_actions import safe_demo_decisions
    report = run_quality_checks(sid, store)
    decisions = safe_demo_decisions(report)
    report = apply_decisions(sid, decisions, store)
    print(
        f"质量处理：{len(decisions)} 个问题按安全默认处理（仅格式转换，其余保留）；"
        f"快照 {report.snapshot_rows} 行\n"
    )
    return sid, report.snapshot_rows


def main(path: str) -> None:
    store = SessionStore()
    sid, snap_rows = _prepare(store, Path(path))

    from app.schemas.dictionary import DataDictionary
    snapshot = store.load_snapshot(sid)
    dictionary = DataDictionary.model_validate(store.read_artifact(sid, "dictionary"))
    bundle = build_bundle(
        dictionary, snapshot, snapshot_rows=snap_rows, title=Path(path).name
    )
    store.write_bundle(sid, bundle.model_dump(mode="json"))
    execution = execute_bundle(sid, store, bundle)
    print(f"Bundle 执行：{execution.succeeded}/{execution.total} 成功，"
          f"{execution.failed} 失败\n")

    artifact = synthesize_dashboard(sid, store)

    print("="  * 72)
    print("DashboardArtifact JSON（完整落盘：bundle/runs/{run_id}/dashboard.json）")
    print("=" * 72)
    print(json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False, indent=2))

    print("\n" + "=" * 72)
    print("KPI 摘要")
    print("=" * 72)
    for k in artifact.kpis:
        val = "null" if k.value is None else (
            f"{k.value * 100:.2f}%" if k.unit == "%" else f"{k.value:,.2f}"
        )
        chg = ""
        # T05：变化口径与引擎一致——正基数才有增长率，其余给差额/状态提示
        if k.change is not None:
            chg += f"｜环比 {k.change * 100:+.1f}%"
        if k.change_delta is not None:
            delta_txt = (
                f"{k.change_delta * 100:+.2f}pp" if k.unit == "%"
                else f"{k.change_delta:+,.2f}"
            )
            chg += f"，差额 {delta_txt}"
        if k.change_status:
            chg += f"（{k.change_status}）"
        if k.change_hint:
            chg += f" · {k.change_hint}"
        print(f"  ■ {k.label}：{val}{chg}")

    print("\n" + "=" * 72)
    print("Sections × Views（真实计算结果）")
    print("=" * 72)
    view_map = {v.view_id: v for v in bundle.analysis_views}
    result_map = {r.view_id: r for r in execution.views}
    for section in artifact.sections:
        print(f"\n■ {section.title}（{section.section_id}）")
        if not section.view_ids:
            print("  （无视角）")
        for vid in section.view_ids:
            v = view_map[vid]
            r = result_map[vid]
            if r.status != ViewStatus.success:
                print(f"  - {vid} [{v.type.value}] {v.title} ✗ {r.reason}")
                continue
            summary = r.steps[-1].summary or {}
            extra = ""
            if "coefficient" in summary:
                extra = f"｜r={summary['coefficient']:.4f}（n={summary.get('n')}）"
            elif v.type is ViewType.profitability:
                import pandas as pd
                res = pd.read_parquet(store.view_dir(sid, vid) / "result.parquet")
                neg = res[res["value"] < 0]
                top = res.head(3)
                top_txt = "，".join(f"{row.iloc[0]}={row['value'] * 100:.1f}%"
                                   for _, row in top.iterrows())
                extra = f"｜Top3 {top_txt}"
                if not neg.empty:
                    extra += f"｜亏损成员 {', '.join(str(x) for x in neg.iloc[:, 0])}"
            print(f"  - {vid} [{v.type.value}] {v.title}（{r.result_rows_total} 行）{extra}")

    print("\n" + "=" * 72)
    print(f"Findings（{len(artifact.findings)} 条，其中 risk {len(artifact.risks)} 条）")
    print("=" * 72)
    for f in artifact.findings:
        print(f"\n■ [{f.importance}] {f.finding_id} {f.title}（{f.type}）")
        print(f"  证据视图：{', '.join(f.evidence_view_ids)}")
        print(f"  {f.summary}")
        if f.drilldown:
            kids = "、".join(
                f"{c.name}（{c.value:,.0f}）"
                for c in f.drilldown.top_negative_children
            )
            print(f"  → 下钻建议：{f.drilldown.filters[0].column}="
                  f"{f.drilldown.filters[0].value} → "
                  f"{f.drilldown.child_dimension}｜Top 负贡献：{kids}")

    print("\n" + "=" * 72)
    print("全局筛选器")
    print("=" * 72)
    for f in artifact.global_filters:
        print(f"  ■ {f.label}：{len(f.members)} 个成员 → {', '.join(f.members[:8])}"
              f"{' …' if len(f.members) > 8 else ''}")

    run_dir = store.active_run_dir(sid)
    probe_root = run_dir / "probes"
    probes = [p for p in probe_root.iterdir()] if probe_root.exists() else []
    print(f"\n运行版本：{artifact.run_id}")
    print(f"探针：{len(probes)} 个（上限 3）→ {probe_root}")
    print(f"产物目录：{run_dir}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法：PYTHONPATH=. uv run python scripts/run_dashboard_demo.py <文件路径>")
        raise SystemExit(1)
    main(sys.argv[1])
