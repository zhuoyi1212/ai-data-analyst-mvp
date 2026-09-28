"""重构 Phase 1 实测脚本：对任意 CSV/Excel 跑完整 Bundle 自动分析并打印结果。

用法（backend 目录下）：
    PYTHONPATH=. uv run python scripts/run_bundle_demo.py <文件路径>

流程：Upload → 语义分析（未确认字段自动采纳当前规则判定）→ 质量处理
（采纳每条问题的智能 suggested_action）→ 生成 AnalysisBundle → 批量执行，
最后打印 Bundle JSON 与每个 View 的真实计算结果摘要。
全程离线规则路径，无 key 无网络；数值只来自确定性引擎。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.schemas.bundle import ViewStatus
from app.services.bundle_executor import execute_bundle
from app.services.bundle_planner import build_bundle
from app.services.profiler import confirm_fields, generate_dictionary
from app.services.quality_actions import apply_decisions
from app.services.quality_checker import run_quality_checks
from app.services.storage import SessionStore


def main(path: str) -> None:
    store = SessionStore()
    file_path = Path(path)
    meta = store.create_from_bytes(file_path.name, file_path.read_bytes())
    sid = meta["session_id"]
    print(f"会话：{sid}")
    print(f"原始数据：{meta['filename']} · {meta['shape']['rows']} 行 × {meta['shape']['cols']} 列\n")

    # 语义：生成后把所有非 high 字段自动确认；unknown 文本列按名称升级为 id/维度
    import re
    dictionary = generate_dictionary(sid, store)
    _ID_HINT = re.compile(r"(id|编码|编号|邮编|code)$", re.I)

    def _auto_semantic(f) -> str:
        if f.semantic_type.value != "unknown":
            return f.semantic_type.value
        if _ID_HINT.search(f.name):
            return "id"
        return "dimension"

    pending = [
        {"name": f.name, "semantic_type": _auto_semantic(f)}
        for f in dictionary.fields
        if not f.confirmed and not f.ignored
    ]
    if pending:
        dictionary = confirm_fields(sid, pending, store)
    pending_names = {p["name"] for p in pending}
    print("数据字典字段：")
    for f in dictionary.fields:
        if f.ignored:
            continue
        how = "用户确认" if f.name in pending_names else "自动确认"
        print(f"  - {f.name}：{f.semantic_type.value}（基数 {f.cardinality}，{how}）")
    print()

    # 质量（T02）：演示路径只自动采纳不改变经营事实的动作（格式转换），
    # 缺失/离群/重复一律保留——删行、插补、去重必须由真实用户显式确认。
    from app.services.quality_actions import safe_demo_decisions
    report = run_quality_checks(sid, store)
    decisions = safe_demo_decisions(report)
    report = apply_decisions(sid, decisions, store)
    print(
        f"质量处理：{len(decisions)} 个问题按安全默认处理（仅格式转换，"
        f"其余保留）；快照 {report.snapshot_rows} 行"
    )
    if report.impact.get("dropped_rows") or report.impact.get("imputed_cells"):
        print("  警告：安全默认不应改变行数/数值，请检查决策来源。")
    print()

    # Bundle 规划 + 执行
    snapshot = store.load_snapshot(sid)
    dictionary = store.read_artifact(sid, "dictionary")
    from app.schemas.dictionary import DataDictionary
    dictionary = DataDictionary.model_validate(dictionary)
    bundle = build_bundle(
        dictionary, snapshot, snapshot_rows=report.snapshot_rows,
        title=meta["filename"],
    )
    store.write_bundle(sid, bundle.model_dump(mode="json"))

    print("=" * 70)
    print("AnalysisBundle JSON")
    print("=" * 70)
    print(json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2))

    summary = execute_bundle(sid, store, bundle)
    print("\n" + "=" * 70)
    print(f"执行汇总：{summary.succeeded}/{summary.total} 成功，{summary.failed} 失败")
    print("=" * 70)
    date_cols = set(dictionary.date_fields())
    for view, result in zip(bundle.analysis_views, summary.views):
        print(f"\n■ {view.view_id} [{view.type.value}] {view.title}")
        print(f"  问题：{view.question}")
        if result.status != ViewStatus.success:
            print(f"  ✗ {result.status.value}：{result.reason}")
            continue
        print(f"  算子：{view.plan.steps[-1].op}｜参与 {result.participating_rows} 行｜"
              f"输出 {result.result_rows_total} 行｜{result.elapsed_ms} ms")
        last = result.steps[-1]
        print(f"  计算口径：{last.formula}")
        stats = last.summary or {}
        if "coefficient" in stats:
            print(f"  → 相关系数 r={stats['coefficient']:.4f}"
                  f"（{stats.get('method', '')}，n={stats.get('n')}）")
        if "outlier_count" in stats:
            print(f"  → 离群 {stats['outlier_count']} 个"
                  f"（{stats['outlier_rate'] * 100:.1f}%），"
                  f"正常区间 [{stats['lower_bound']:.2f}, {stats['upper_bound']:.2f}]")
        # anomaly 全量结果含全部原始列，预览只保留 日期+主维度+指标+标记
        keep: list[str] | None = None
        if view.type.value == "anomaly":
            keep = [*date_cols, *bundle.primary_dimensions[:2],
                    *view.metric_fields, "is_outlier"]
        preview = result.result_preview[:8]
        for row in preview:
            items = ([(k, row[k]) for k in keep if k in row]
                     if keep is not None else row.items())
            cells = " | ".join(f"{k}={_fmt(v)}" for k, v in items)
            print(f"    {cells}")
        if result.result_rows_total > len(preview):
            print(f"    … 共 {result.result_rows_total} 行")
        warns = [c for c in result.checks if c.level in ("warn", "fail")]
        for c in warns:
            print(f"  ⚠ {c.code}: {c.detail}")

    print(f"\n产物目录：{store.bundle_dir(sid)}")


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.2f}"
    return v


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法：PYTHONPATH=. uv run python scripts/run_bundle_demo.py <文件路径>")
        raise SystemExit(1)
    main(sys.argv[1])
