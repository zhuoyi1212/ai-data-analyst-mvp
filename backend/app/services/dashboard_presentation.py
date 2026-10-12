"""紧凑展示：按口径、范围与结果去重，证据 ID 保留为选择器成员的别名。"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any

from app.schemas.common import TimeGranularity
from app.schemas.dashboard import CollapseGroup, CollapseMember, ViewCard
from app.services.field_labels import field_label


def _scope(steps: list[dict]) -> tuple[str, str]:
    filters = [step["params"] for step in steps if step.get("op") == "filter"]
    normalized = []
    labels = []
    for params in filters:
        value = params.get("value")
        if isinstance(value, list) and params.get("operator") == "in":
            value = sorted(value, key=str)
        normalized.append({"column": params["column"], "operator": params["operator"], "value": value})
        rendered = "、".join(map(str, value)) if isinstance(value, list) else str(value)
        operator = "=" if params["operator"] in ("in", "==") else f" {params['operator']} "
        labels.append(f"{field_label(params['column'])}{operator}{rendered}")
    normalized.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str), " · ".join(labels) or "全部数据"


def enrich_cards(cards: dict[str, ViewCard], bundle=None, *, store=None, session_id="", run_id=""):
    plans = {view.view_id: view.plan.model_dump(mode="json") for view in bundle.analysis_views} if bundle else {}
    out = {}
    for view_id, card in cards.items():
        plan = plans.get(view_id)
        if card.ref_type == "probe" and store is not None:
            path = store.run_dir(session_id, run_id) / "diagnostic" / "probes" / view_id / "plan_group_by.json"
            if path.exists():
                plan = json.loads(path.read_text())
        if not plan:
            out[view_id] = card
            continue
        scope_key, scope_label = _scope(plan["steps"])
        spec = card.chart_spec
        if spec:
            last = plan["steps"][-1]
            spec = spec.model_copy(update={
                "aggregation": last["params"].get("func"),
                "time_granularity": TimeGranularity(last["params"]["granularity"]) if last["op"] == "time_series" else spec.time_granularity,
            })
        out[view_id] = card.model_copy(update={"scope_key": scope_key, "scope_label": scope_label, "chart_spec": spec})
    return out


def _metric(card: ViewCard) -> str:
    if card.type == "profitability" and card.data:
        return next((col.label for col in card.data.columns if col.name == "value" and col.label), field_label(card.chart_spec.metric or card.metric_label))
    return field_label(card.metric_label or card.chart_spec.metric or "指标")


def build_collapse_groups(cards: dict[str, ViewCard]) -> list[CollapseGroup]:
    buckets = defaultdict(list)
    for card in cards.values():
        spec = card.chart_spec
        if not card.consumable or not spec:
            continue
        # Only executed probe scopes are safe to combine with whole-table charts.
        if card.ref_type == "probe" and not card.scope_key:
            continue
        if card.ref_type == "view" and card.role == "computation" and card.type not in ("comparison", "breakdown", "profitability"):
            continue
        metric = spec.metric or card.metric_label
        if not metric:
            continue
        if card.type == "trend" and spec.type == "line":
            key = ("trend", spec.x_field, getattr(spec.time_granularity, "value", spec.time_granularity), spec.aggregation)
        elif card.type == "ranking" and spec.dimension:
            key = ("ranking", spec.dimension, spec.aggregation)
        elif spec.dimension and card.type in ("comparison", "breakdown", "profitability") and spec.type in ("bar", "share_bar", "pie", "grouped_bar"):
            key = ("dimension", metric, spec.aggregation)
        else:
            continue
        buckets[key].append(card)

    groups = []
    for key, variants in buckets.items():
        if len(variants) < 2:
            continue
        kind = key[0]
        members: list[CollapseMember] = []
        canonical: dict[tuple, CollapseMember] = {}
        for card in sorted(variants, key=lambda c: (c.scope_label != "全部数据", c.ref_type == "probe", c.view_id)):
            spec = card.chart_spec
            dimension = field_label(spec.dimension or spec.x_field or "日期")
            metric_label = _metric(card)
            measure = "占比" if spec.type in ("pie", "share_bar") else "比率" if card.type == "profitability" else "金额"
            variant = ""
            if kind == "ranking":
                match = re.search(r"\b(Top|Bottom)\s*(\d+)\b", card.title, re.I)
                variant = f"{'前' if match[1].lower() == 'top' else '后'} {match[2]}" if match else card.title
            rows = card.data.rows if card.data else []
            field = "share" if measure == "占比" else "value"
            plotted = [(str(row.get(spec.x_field or spec.dimension or "")), row.get(field)) for row in rows]
            if kind == "dimension":
                plotted.sort(key=lambda row: row[0])
            signature = (metric_label, dimension, card.scope_key, measure, variant, json.dumps(plotted, default=str))
            if signature in canonical:
                canonical[signature].view_ids.append(card.view_id)
                continue
            member = CollapseMember(view_id=card.view_id, view_ids=[card.view_id], dimension_label=dimension,
                                    metric_label=metric_label, scope_label=card.scope_label,
                                    measure_label=measure, variant_label=variant)
            canonical[signature] = member
            members.append(member)
        if kind == "trend":
            labels = {"month": "月份趋势", "week": "每周趋势", "day": "每日趋势", "quarter": "季度趋势", "year": "年度趋势"}
            label = labels.get(key[2], "时间趋势")
        elif kind == "ranking":
            label = f"{field_label(key[1])}排名"
        else:
            label = f"{_metric(variants[0])}对比"
            if key[2] and key[2] != "sum":
                label += f"（{ {'mean':'均值','median':'中位数','count':'计数','max':'最大值','min':'最小值'}.get(key[2],key[2]) }）"
        digest = hashlib.sha1(json.dumps(key, default=str).encode()).hexdigest()[:10]
        groups.append(CollapseGroup(group_id=f"collapse_{digest}", kind=kind, label=label, members=members))
    return groups
