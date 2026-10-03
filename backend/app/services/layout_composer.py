"""Tableau 风格 12-column 布局合成（Task 7）。

输入 DashboardArtifact 的组成部件（KPI、视图/探针卡、Findings、深挖链），
输出 DashboardLayout：KPI Strip → Hero → Primary → Supporting →
Diagnostic → 折叠证据 → Findings。

纯确定性：
- 视图优先级由 value_scores 加权（同 view_id 决胜），不引入 LLM；
- 连续顺序贪心换行：每行 col_span 之和 ≤ 12（前端同规则渲染）；
- 每个排版项带非空 rationale；低优先级探针/隐藏卡 default_hidden 可展开。
"""
from __future__ import annotations

from typing import Any

from app.schemas.dashboard import (
    DashboardLayout,
    DashboardLayoutItem,
    ViewCard,
)
from app.services.diagnostic_search import _now

# 可见深挖探针数量（每行 2 个 ×2 行）
VISIBLE_PROBES = 4
HERO_ROW_SPAN = 2


def _priority(card: ViewCard) -> float:
    s = card.value_scores
    return (
        0.45 * s.get("impact", 0.0)
        + 0.25 * s.get("evidence", 0.0)
        + 0.15 * s.get("novelty", 0.0)
        + 0.15 * s.get("validity", 0.0)
        - 0.20 * s.get("redundancy", 0.0)
        - 0.10 * s.get("cost", 0.0)
    )


def _probe_order(chain_set: Any) -> tuple[list[str], dict[str, str]]:
    """按 chains 顺序去重 probe_id；并记录 probe 归属 chain。"""
    order: list[str] = []
    owner: dict[str, str] = {}
    if chain_set is None:
        return order, owner
    for chain in chain_set.chains:
        for dv in chain.diagnostic_views:
            pid = dv.probe_id
            if pid not in owner:
                owner[pid] = chain.chain_id
                order.append(pid)
    return order, owner


def compose_layout(
    *,
    kpis: list[Any],
    cards: dict[str, ViewCard],
    findings: list[Any],
    chain_set: Any = None,
) -> DashboardLayout:
    """由 artifact 部件确定性组合布局。"""
    raw: list[DashboardLayoutItem] = []

    # 1) KPI Strip：等宽，n*span ≤12
    n_kpi = len(kpis)
    if n_kpi:
        kpi_span = min(6, 12 // n_kpi)
        for i, kpi in enumerate(kpis, start=1):
            raw.append(DashboardLayoutItem(
                item_id=f"kpi_{i:02d}", role="kpi", ref_type="meta",
                col_span=kpi_span, row_span=1, order=0,
                rationale=f"KPI 条：{kpi.label}",
            ))

    # 2) 普通视图：可消费、默认可见，按价值排序
    view_candidates = sorted(
        (c for c in cards.values()
         if c.ref_type == "view" and c.consumable and not c.default_hidden),
        key=lambda c: (-_priority(c), c.view_id),
    )

    def _add(
        card: ViewCard, role: str, span: int, *,
        row_span: int = 1, rationale: str, hidden: bool = False,
    ) -> None:
        raw.append(DashboardLayoutItem(
            item_id=card.view_id, role=role, ref_type="view",
            col_span=span, row_span=row_span, order=len(raw),
            rationale=rationale, default_hidden=hidden,
        ))

    if view_candidates:
        hero = view_candidates[0]
        _add(
            hero, "hero", 12, row_span=HERO_ROW_SPAN,
            rationale=(
                f"综合价值最高的视图（优先级 {_priority(hero):.2f}），"
                "作为主图全宽展示。"
            ),
        )
        for card in view_candidates[1:3]:
            _add(
                card, "primary", 6,
                rationale=(
                    f"高优先级视图（优先级 {_priority(card):.2f}），"
                    "与主图互补的核心视角。"
                ),
            )
        for card in view_candidates[3:7]:
            _add(
                card, "supporting", 4,
                rationale=(
                    f"支撑视图（优先级 {_priority(card):.2f}），"
                    "补充其他维度与结构信息。"
                ),
            )
        for card in view_candidates[7:]:
            _add(
                card, "supporting", 4,
                rationale=(
                    f"次级支撑视图（优先级 {_priority(card):.2f}），"
                    "优先级较低，默认折叠可展开。"
                ),
                hidden=True,
            )

    # 3) 深挖探针（去重、按 chain 顺序）：前 4 个可见，其余折叠
    probe_ids, owner = _probe_order(chain_set)
    for i, pid in enumerate(probe_ids):
        card = cards.get(pid)
        if card is None:
            continue
        visible = i < VISIBLE_PROBES
        raw.append(DashboardLayoutItem(
            item_id=pid, role="diagnostic", ref_type="probe",
            col_span=6, row_span=1, order=len(raw),
            rationale=(
                f"深挖链 {owner[pid]} 的诊断视图"
                + ("，对高价值信号的逐层拆解。" if visible
                   else "，优先级较低，默认折叠可展开。")
            ),
            default_hidden=not visible,
        ))

    # 4) 默认隐藏卡（computation / 低价值视图，尚未放置的）
    placed = {item.item_id for item in raw}
    hidden_cards = sorted(
        (c for c in cards.values()
         if c.view_id not in placed and c.consumable),
        key=lambda c: (-_priority(c), c.view_id),
    )
    for card in hidden_cards:
        reason = "；".join(card.hide_reasons) or "价值评估较低"
        raw.append(DashboardLayoutItem(
            item_id=card.view_id, role="supporting", ref_type="view",
            col_span=4, row_span=1, order=len(raw),
            rationale=f"默认折叠的证据视图（{reason}），可按需展开。",
            default_hidden=True,
        ))

    # 5) Key Findings 条
    if findings:
        raw.append(DashboardLayoutItem(
            item_id="findings", role="findings", ref_type="meta",
            col_span=12, row_span=1, order=len(raw),
            rationale=f"关键发现清单（共 {len(findings)} 条）。",
        ))

    # order 重排为最终连续序（构造期间已递增，此处显式固化）
    items = [
        item.model_copy(update={"order": i}) for i, item in enumerate(raw)
    ]
    return DashboardLayout(items=items, generated_at=_now())
