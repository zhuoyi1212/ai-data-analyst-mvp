"""用真实 LLM 录制某份示例数据的全套回放固件（Task 17 / Task 24 工具）。

用法（在 backend/ 目录，需先在 .env 配置 LLM_API_KEY）：
    LLM_FIXTURE_MODE=record PYTHONPATH=. \
        uv run python scripts/record_live_pipeline.py sales_orders

录制前会删除该数据集既有的 5 阶段固件，避免追加污染。
录制后请运行 `uv run pytest tests/test_e2e_pipeline.py -k sales_orders`，
若新固件无法通过确定性门禁，说明模型输出不可靠——这正是本产品要暴露的问题，
请重新录制或调整提示词，不得手改放行。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from app.config import settings
from scripts.build_e2e_fixtures import DATASET_META
from scripts.generate_sample_data import DATASETS_CONFIG, GOLDEN_PATH


def main() -> None:
    if settings.fixture_mode != "record":
        raise SystemExit("必须以 LLM_FIXTURE_MODE=record 运行本脚本。")
    if not settings.llm_api_key:
        raise SystemExit("未检测到 LLM_API_KEY，请先在 backend/.env 配置。")
    if len(sys.argv) != 2 or sys.argv[1] not in DATASETS_CONFIG:
        raise SystemExit(f"用法：record_live_pipeline.py {{{'|'.join(DATASETS_CONFIG)}}}")
    name = sys.argv[1]

    # 延迟导入：确保 record 模式设置先生效
    from app.services.executor import execute
    from app.services.followup import generate_followups
    from app.services.insight import generate_insights
    from app.services.planner import confirm_plan, generate_plan
    from app.services.profiler import confirm_fields, generate_dictionary
    from app.services.quality_actions import apply_decisions
    from app.services.quality_checker import run_quality_checks
    from app.services.recommender import recommend
    from app.services.storage import SessionStore
    from app.services.validator import acknowledge, run_validation

    # 1) 清理旧固件
    removed = []
    for stage, suffix in [("profile", ""), ("questions", "_questions"),
                          ("plan", "_plan"), ("insights", "_insights"),
                          ("followup", "_followup")]:
        p = settings.fixture_dir / stage / f"{name}{suffix}.json"
        if p.exists():
            p.unlink()
            removed.append(str(p.relative_to(settings.fixture_dir)))
    print(f"已清理旧固件：{removed or '无'}")

    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["datasets"][name]
    semantics = DATASETS_CONFIG[name]["semantics"]

    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "record-store")
        sid = store.create_from_sample(golden["filename"])["session_id"]

        print("▶ 录制 profile …")
        dictionary = generate_dictionary(sid, store)
        if not dictionary.complete:
            decisions = [
                {"name": f.name, "semantic_type": semantics[f.name].value,
                 "meaning": f.meaning or "录制脚本自动确认"}
                for f in dictionary.fields
                if not (f.confidence.value == "high"
                        and f.semantic_type.value != "unknown")
            ]
            confirm_fields(sid, decisions, store)
            print(f"  自动确认 {len(decisions)} 个非 high 字段")

        print("▶ 质量检测与处理（沿用黄金决策）…")
        run_quality_checks(sid, store)
        apply_decisions(sid, golden["decisions"], store)

        print("▶ 录制 questions …")
        qs = recommend(sid, store)

        print("▶ 录制 plan（推荐问题首条）…")
        generate_plan(sid, qs.questions[0].text, store)
        confirm_plan(sid, store)

        print("▶ 执行 + 校验（无 LLM）…")
        execute(sid, store)
        report = run_validation(sid, store)
        if report.overall == "fail":
            raise SystemExit("规范方案校验 fail，模型生成的方案不可靠，已终止录制。")
        if report.overall == "warn":
            acknowledge(sid, store)
            print("  校验存在 warn，已按流程知悉后继续。")

        print("▶ 录制 insights …")
        generate_insights(sid, store)

        print("▶ 录制 followup …")
        generate_followups(sid, store)

    print(f"✓ {name} 录制完成，固件位于 {settings.fixture_dir}")
    print("  请运行：uv run pytest tests/test_e2e_pipeline.py -k " + name)


if __name__ == "__main__":
    main()
