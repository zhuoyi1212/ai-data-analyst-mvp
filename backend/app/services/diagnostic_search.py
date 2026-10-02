"""预算受控的 Diagnostic Search（T09）。

单控制器在当前运行（run）内做深度受限的自适应维度诊断：
    root
     ├ Round 1：≤3 个候选维度，每维 1 probe → 保留 ≤2 分支
     ├ Round 2+：只在当前最有价值的 open 叶节点试探 1 个新维度
     └ 深度 ≤3

预算硬边界（失败也耗预算；恢复不重置；search.json 是唯一事实）：
    rounds ≤ 4，probes ≤ 8，每 probe plans ≤ 3，终端执行 ≤ 24。

红线：数值只来自确定性算子；LLM 只选试探维度（≤4 次）与最终综合（1 次），
不产数字/代码/公式；评分是启发式排序，不是因果解释度或统计信息增益。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.bundle import AnalysisBundle
from app.schemas.diagnostic import (
    DiagnosticSearch,
    PlanBrief,
    ProbeRecord,
    ProbeScores,
    RoundRecord,
    ScopeFilter,
    SearchBudget,
    SearchNode,
    StopReason,
)
from app.schemas.dictionary import DataDictionary
from app.schemas.plan import (
    AnalysisPlan,
    FilterParams,
    FilterStep,
)
from app.services.bundle_planner import (
    _spec_func,
    apply_scope_filters,
    normalize_scope,
)
from app.services.derived_metrics import detect_derived_metrics
from app.services.engine.ops import EngineError
from app.services.executor import execute_plan
from app.services.llm import generate_json
from app.services.llm.errors import ContractError
from app.services.llm.fixtures import FixtureMissingError
from app.services.metric_spec import effective_metric_spec
from app.services.offline_fallback import (
    _ranked_dimensions,
    build_plan,
)
from app.services.storage import SessionStore, StaleRunError, StorageError

# ---------------------------------------------------------------- 常量

EXPAND_THRESHOLD = 0.25     # probe total 低于此不展开
WEAK_IMPACT = 0.10          # impact 低于此立即按影响不足停止
MIN_GROUP_ROWS = 5          # 每组最小行数
MIN_GROUP_ENTITIES = 3      # 有业务键时每组最小独立实体数
RATE_IMPACT_FULL_PP = 5.0   # 率变化 5pp 为 impact 满分
DELTA_IMPACT_FULL = 0.20    # 总量变化 20% 为 impact 满分

# total 权重
W_IMPACT = 0.30
W_CONCENTRATION = 0.20
W_STABILITY = 0.15
W_SUPPORT = 0.15
W_NOVELTY = 0.20

ROOT_ID = "root"

_STAGE = "diagnostic"

_SYSTEM_PICK = (
    "你是诊断搜索的维度选择器。根据数据字典中的字段语义、基数与根问题，"
    "选择最值得试探的维度。只能从给定候选维度中选择，禁止生成字段以外的内容，"
    "不要输出数字或计算。仅输出 JSON。"
)

_SYSTEM_SYNTHESIS = (
    "你是诊断结论综合器。基于给定的结构化观察（全部来自确定性计算），"
    "写出简短结论；只能引用 evidence_probe_ids 中真实存在的探针，"
    "不得补写任何业务数字，不得表述为因果结论。仅输出 JSON。"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ================================================================ LLM 输出契约


class _DimensionPick(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_dimensions: list[str] = Field(min_length=1)
    rationale: str = ""


class _Synthesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1)
    evidence_probe_ids: list[str] = Field(default_factory=list)


# ================================================================ 路径 / plan 构造


def _resolve_values(
    df: pd.DataFrame, column: str, str_values: list[str]
) -> list[Any]:
    """把展示用字符串成员还原为该列的原始类型值（数字维度安全）。"""
    series = df[column]
    present = {str(v): v for v in series.dropna().unique()}
    return [present[s] for s in str_values if s in present]


def _filter_steps(df: pd.DataFrame, scopes: list[ScopeFilter]) -> list[FilterStep]:
    steps: list[FilterStep] = []
    for i, scope in enumerate(scopes):
        raw_values = _resolve_values(df, scope.column, scope.values)
        if not raw_values:
            continue
        steps.append(FilterStep(
            step_id=f"scope_filter_{i + 1}",
            op="filter",
            description=f"继承父节点范围：「{scope.column}」限定为 {scope.values}",
            params=FilterParams(
                column=scope.column, operator="in", value=raw_values
            ),
        ))
    return steps


def _scoped_plan(
    question: str, op: str, hint: dict[str, Any],
    fields: list[str], df: pd.DataFrame, scopes: list[ScopeFilter],
) -> AnalysisPlan:
    """带父节点 filter 链的终端计划（先继承范围，再执行末端算子）。"""
    plan = build_plan(question, op, hint, fields)
    return plan.model_copy(
        update={"steps": _filter_steps(df, scopes) + plan.steps}
    )


# ================================================================ 评分（启发式）


def _group_sizes(df: pd.DataFrame, dim: str) -> pd.Series:
    """各组成员行数（仅用于评分，不产出业务数字）。"""
    return df.groupby(dim, dropna=False).size()


def _score_concentration(values: pd.Series, n_groups: int) -> float:
    """HHI 相对均匀分布的超额：均匀=0，单组垄断=1。"""
    if n_groups <= 1:
        return 0.0
    v = values.abs()
    total = float(v.sum())
    if total <= 0:
        return 0.0
    hhi = float(((v / total) ** 2).sum())
    uniform = 1.0 / n_groups
    return max(0.0, min(1.0, (hhi - uniform) / (1.0 - uniform)))


def _score_support(
    df: pd.DataFrame, dim: str, spec
) -> tuple[float, int, int]:
    """最小组支撑度；返回 (score, min_rows, min_entities)。"""
    sizes = _group_sizes(df, dim)
    min_rows = int(sizes.min()) if len(sizes) else 0
    min_entities = min_rows
    if spec is not None and getattr(spec, "entity_key", None):
        key = spec.entity_key
        if key in df.columns:
            entities = df.groupby(dim)[key].nunique()
            min_entities = int(entities.min()) if len(entities) else 0
            return min(1.0, min_entities / MIN_GROUP_ENTITIES), min_rows, min_entities
    return min(1.0, min_rows / MIN_GROUP_ROWS), min_rows, min_entities


def _cardinality_penalty(card: int) -> float:
    if card <= 30:
        return 0.0
    return min(0.5, (card - 30) / 140.0)


def _score_probe(
    *,
    df: pd.DataFrame,
    dim: str,
    spec,
    card: int,
    gdf: pd.DataFrame | None,
    cdf: pd.DataFrame | None,
    csumm: dict[str, Any] | None,
    rsumm: dict[str, Any] | None,
    used_dims_in_bundle: set[str],
    used_dims_in_search: set[str],
) -> ProbeScores:
    groups = int(gdf.shape[0]) if gdf is not None else 0

    concentration = (
        _score_concentration(gdf["value"], groups)
        if gdf is not None and "value" in gdf.columns and groups > 0 else 0.0
    )

    # impact：优先变化口径
    impact = 0.0
    if csumm is not None:
        base = abs(float(csumm.get("total_base_value", 0.0)))
        delta = abs(float(csumm.get("total_delta", 0.0)))
        if base > 1e-9:
            impact = min(1.0, (delta / base) / DELTA_IMPACT_FULL)
        elif delta > 1e-9:
            impact = 1.0
    if rsumm is not None:
        pp = abs(float(rsumm.get("change_pp", 0.0)))
        impact = max(impact, min(1.0, pp / RATE_IMPACT_FULL_PP))
    if impact == 0.0 and gdf is not None and groups > 1:
        vals = gdf["value"].abs()
        spread = float(vals.max() - vals.min())
        total = float(vals.sum())
        if total > 1e-9:
            impact = min(1.0, (spread / total) / 0.5)

    support, _, _ = _score_support(df, dim, spec)

    # stability：基期→当前结构权重的变化度（0 稳定，1 洗牌）；仅当前快照时给中性 0.5
    stability = 0.5
    if cdf is not None and "base_value" in cdf and "current_value" in cdf:
        cur = pd.to_numeric(cdf["current_value"], errors="coerce").abs()
        base = pd.to_numeric(cdf["base_value"], errors="coerce").abs()
        w1 = cur / max(float(cur.sum()), 1e-9)
        w0 = base / max(float(base.sum()), 1e-9)
        stability = float(0.5 * (w1.fillna(0) - w0.fillna(0)).abs().sum())

    # novelty：与 bundle 现有视图、搜索树已展开分支的维度重叠 → 降权
    novelty = 1.0
    if dim in used_dims_in_bundle:
        novelty = 0.4
    if dim in used_dims_in_search:
        novelty = min(novelty, 0.4)
    penalty = _cardinality_penalty(card)

    raw = (
        W_IMPACT * impact + W_CONCENTRATION * concentration
        + W_STABILITY * stability + W_SUPPORT * support
        + W_NOVELTY * novelty
    )
    total = max(0.0, min(1.0, raw - penalty))
    return ProbeScores(
        concentration=round(concentration, 4),
        impact=round(impact, 4),
        support=round(support, 4),
        stability=round(stability, 4),
        novelty=round(novelty, 4),
        cardinality_penalty=round(penalty, 4),
        total=round(total, 4),
    )


# ================================================================ 控制器


class DiagnosticController:
    def __init__(
        self,
        session_id: str,
        store: SessionStore,
        search: DiagnosticSearch,
        *,
        dictionary: DataDictionary,
        scope_snapshot: pd.DataFrame,
        metric: str,
        func: str,
        date_col: str | None,
        bundle: AnalysisBundle,
    ):
        self.sid = session_id
        self.store = store
        self.search = search
        self.dictionary = dictionary
        self.df = scope_snapshot
        self.metric = metric
        self.func = func
        self.date_col = date_col
        self.bundle = bundle
        # probe_id → {op: 结果 DataFrame}：仅当轮内存中使用（展开/评分），
        # 恢复后节点状态已定，无需重建。
        self._results: dict[str, dict[str, pd.DataFrame]] = {}

    # ------------------------------------------------ 持久化 / 查询
    def _persist(self) -> None:
        self.search.updated_at = _now()
        self.store.write_diagnostic(
            self.sid, self.search.model_dump(mode="json")
        )

    def _node(self, node_id: str) -> SearchNode | None:
        return next((n for n in self.search.nodes if n.node_id == node_id), None)

    def _node_probes(self, node_id: str) -> list[ProbeRecord]:
        return [p for p in self.search.probes if p.node_id == node_id]

    def _children(self, node_id: str) -> list[SearchNode]:
        return [n for n in self.search.nodes if n.parent_id == node_id]

    @property
    def budget(self) -> SearchBudget:
        return self.search.budget

    def _scoped_df(self, node: SearchNode) -> pd.DataFrame:
        """按节点继承链裁剪（与 probe 中 filter 步骤同一口径）。"""
        out = self.df
        for scope in node.scope_filters:
            raw_values = _resolve_values(out, scope.column, scope.values)
            if not raw_values:
                return out.iloc[0:0]
            out = out[out[scope.column].isin(raw_values)]
        return out

    # ------------------------------------------------ 维度候选
    def _candidate_dims(self, node: SearchNode) -> list:
        blocked = {s.column for s in node.scope_filters}
        tried = {p.dimension for p in self._node_probes(node.node_id)}
        out = []
        for f in _ranked_dimensions(self.dictionary):
            if f.name in blocked or f.name in tried:
                continue
            if f.cardinality is None or f.cardinality < 2:
                continue  # 拒绝单成员 / 未知基数
            if f.name not in self.df.columns:
                continue
            out.append(f)
        return out

    def _llm_pick_dims(self, candidates: list, max_pick: int) -> list[str]:
        """LLM 从候选中选择维度（≤4 次/搜索）；无 key/固件失败→规则取前 N。"""
        names = [f.name for f in candidates]
        payload = {
            "root_question": self.search.root_question,
            "candidate_dimensions": [
                {"name": f.name, "cardinality": f.cardinality,
                 "semantic_type": f.semantic_type.value}
                for f in candidates
            ],
            "pick_count": max_pick,
            "output": '{"selected_dimensions":[...], "rationale": "..."}',
        }
        try:
            picked = generate_json(
                stage=_STAGE,
                fixture_name=(
                    f"pick_r{self.budget.used_rounds + 1}_{self.sid[:8]}"
                ),
                system=_SYSTEM_PICK,
                user=json.dumps(payload, ensure_ascii=False),
                schema=_DimensionPick,
                business_validator=lambda o: (
                    [] if set(o.selected_dimensions) <= set(names)
                    else [f"选择了候选之外的维度：{set(o.selected_dimensions) - set(names)}"]
                ),
            )
            dims = [d for d in picked.selected_dimensions if d in names]
            return dims[:max_pick] or names[:max_pick]
        except (FixtureMissingError, ContractError):
            return names[:max_pick]

    # ------------------------------------------------ 单个 probe
    def _terminal_plans(
        self, node: SearchNode, dim: str
    ) -> list[tuple[str, dict, AnalysisPlan]]:
        """1–3 个终端计划（不凑数）：group_by → contribution → rate_decomposition。"""
        plans: list[tuple[str, dict, AnalysisPlan]] = []

        # 1) 分组聚合（当前分组值 / 集中度 / support 基础）
        hint = {"dimension": dim, "metric": self.metric, "func": self.func,
                "order": "desc"}
        plans.append(("group_by", hint, _scoped_plan(
            f"在当前范围内，「{self.metric}」按「{dim}」如何分布？",
            "group_by", hint, [dim, self.metric], self.df, node.scope_filters,
        )))

        # 2) 变化贡献（口径合格：可加 sum；count_distinct 需 entity_key）
        field = next(
            (f for f in self.dictionary.fields if f.name == self.metric), None
        )
        spec = effective_metric_spec(field) if field else None
        eligible = (
            spec is not None and spec.additive and spec.aggregation == "sum"
        ) or (
            spec is not None and spec.aggregation == "count_distinct"
            and bool(spec.entity_key)
        )
        if eligible and self.date_col:
            hint = {
                "date_column": self.date_col, "metric": self.metric,
                "dimension": dim, "func": self.func,
                "period": self.search.period,
            }
            plans.append(("contribution", hint, _scoped_plan(
                f"「{self.metric}」的变化按「{dim}」如何贡献？",
                "contribution", hint,
                [self.date_col, self.metric, dim],
                self.df, node.scope_filters,
            )))

        # 3) 率结构分解（存在分子/分母双字段；需要时间列做两期对比）
        if self.date_col:
            rate_specs = detect_derived_metrics(self.dictionary)
            if rate_specs:
                rspec = rate_specs[0]
                hint = {
                    "date_column": self.date_col,
                    "numerator": rspec.numerator,
                    "denominator": rspec.denominator,
                    "dimension": dim, "period": self.search.period,
                }
                plans.append(("rate_decomposition", hint, _scoped_plan(
                    f"「{rspec.numerator}/{rspec.denominator}」按「{dim}」的结构变化？",
                    "rate_decomposition", hint,
                    [self.date_col, rspec.numerator, rspec.denominator, dim],
                    self.df, node.scope_filters,
                )))
        return plans[: self.budget.max_plans_per_probe]

    def _run_probe(self, node: SearchNode, dim: str) -> ProbeRecord:
        probe_id = f"probe_{uuid.uuid4().hex[:10]}"
        briefs: list[PlanBrief] = []
        observations: dict[str, Any] = {}
        result_tables: dict[str, pd.DataFrame] = {}
        consumable_plans = 0

        for op, hint, plan in self._terminal_plans(node, dim):
            # 终端执行预算（成功/失败都计数）
            if self.budget.executions_left() <= 0:
                briefs.append(PlanBrief(
                    op=op, params=hint, status="skipped",
                    reason="终端执行预算已耗尽",
                ))
                continue
            self.budget.used_terminal_executions += 1
            try:
                out_df, records, _ = execute_plan(self.df, plan)
                if len(out_df.index) == 0:
                    briefs.append(PlanBrief(
                        op=op, params=hint, status="failed",
                        reason="结果无有效行",
                    ))
                    continue
                consumable_plans += 1
                result_tables[op] = out_df
                self.store.write_diagnostic_probe_plan(
                    self.sid, probe_id, op, plan.model_dump(mode="json")
                )
                self.store.save_diagnostic_probe_result(
                    self.sid, probe_id, op, out_df
                )
                self.store.write_diagnostic_probe_ledger(
                    self.sid, probe_id, op, {
                        "op": op,
                        "steps": [
                            r.model_dump(mode="json") for r in records
                        ],
                    }
                )
                briefs.append(PlanBrief(op=op, params=hint, status="executed"))
                observations[op] = records[-1].summary
            except EngineError as e:
                briefs.append(PlanBrief(
                    op=op, params=hint, status="failed", reason=str(e)
                ))
            except Exception as e:  # noqa: BLE001 —— probe 内异常收敛
                briefs.append(PlanBrief(
                    op=op, params=hint, status="failed",
                    reason=f"{type(e).__name__}: {e}",
                ))

        executed = sum(1 for b in briefs if b.status == "executed")
        failed = sum(1 for b in briefs if b.status == "failed")
        record = ProbeRecord(
            probe_id=probe_id, node_id=node.node_id,
            round=self.budget.used_rounds + 1, dimension=dim,
            scope_filters=node.scope_filters, plans=briefs,
            executed_count=executed, failed_count=failed,
            consumable=consumable_plans > 0,
            observations=observations,
            failed_reason="" if executed else "该维度试探的全部终端计划均未成功。",
        )

        # 评分：support/集中度必须在节点 scope 内计算
        dim_field = next(
            (f for f in self.dictionary.fields if f.name == dim), None
        )
        metric_field = next(
            (f for f in self.dictionary.fields if f.name == self.metric), None
        )
        spec = effective_metric_spec(metric_field) if metric_field else None
        card = int(dim_field.cardinality or 0) if dim_field else 0
        scoped = self._scoped_df(node)
        used_in_bundle = {
            d for v in self.bundle.analysis_views
            for d in v.dimension_fields
        }
        used_in_search = {
            n.dimension for n in self.search.nodes
            if n.parent_id is not None and n.dimension
        }
        record.scores = _score_probe(
            df=scoped, dim=dim, spec=spec, card=card,
            gdf=result_tables.get("group_by"),
            cdf=result_tables.get("contribution"),
            csumm=observations.get("contribution"),
            rsumm=observations.get("rate_decomposition"),
            used_dims_in_bundle=used_in_bundle,
            used_dims_in_search=used_in_search,
        )

        self._results[probe_id] = result_tables
        self.budget.used_probes += 1
        self.search.probes.append(record)
        self._persist()
        return record

    # ------------------------------------------------ 展开 / 节点判定
    def _key_member(self, probe: ProbeRecord) -> str | None:
        tables = self._results.get(probe.probe_id, {})
        dim = probe.dimension
        cdf = tables.get("contribution")
        if cdf is not None and "delta" in cdf:
            return str(
                cdf.sort_values("delta", key=lambda s: s.abs()).iloc[-1][dim]
            )
        gdf = tables.get("group_by")
        if gdf is not None and "value" in gdf:
            return str(
                gdf.sort_values("value", key=lambda s: s.abs()).iloc[-1][dim]
            )
        return None

    def _expand_child(self, node: SearchNode, probe: ProbeRecord) -> SearchNode:
        member = self._key_member(probe) or ""
        child = SearchNode(
            node_id=f"node_{uuid.uuid4().hex[:10]}",
            parent_id=node.node_id,
            depth=node.depth + 1,
            scope_filters=node.scope_filters + [
                ScopeFilter(column=probe.dimension, values=[member])
            ],
            dimension=probe.dimension,
            state="open",
            scores=probe.scores,
        )
        self.search.nodes.append(child)
        node.state = "expanded"
        return child

    def _evaluate_after_probe(self, node: SearchNode, probe: ProbeRecord) -> None:
        """根据 probe 结果更新节点（展开 / 停止）。"""
        scores = probe.scores
        good = (
            probe.consumable and scores is not None
            and scores.total >= EXPAND_THRESHOLD
        )
        if good:
            if node.depth >= self.budget.max_depth:
                node.state = "stopped"
                node.stop_reason = "depth_exhausted"
            else:
                self._expand_child(node, probe)
            return

        # 未展开：立即停止类判定
        if not probe.consumable:
            sizes = _group_sizes(self._scoped_df(node), probe.dimension)
            if len(sizes) < 2 or int(sizes.min()) < 2:
                node.state = "stopped"
                node.stop_reason = "insufficient_data"
                return
        if scores is not None and scores.impact < WEAK_IMPACT:
            node.state = "stopped"
            node.stop_reason = "insufficient_impact"
            return

        # 其余保留 open；同节点连续两次试探无新分支即停止
        history = self._node_probes(node.node_id)
        if len(history) >= 2:
            if all(not p.consumable for p in history[-2:]):
                node.state = "stopped"
                node.stop_reason = "branch_failed_2x"
            else:
                node.state = "stopped"
                node.stop_reason = "no_new_evidence_2x"

    # ------------------------------------------------ 轮次
    def _open_leaves(self) -> list[SearchNode]:
        return [
            n for n in self.search.nodes
            if n.state == "open" and not self._children(n.node_id)
        ]

    @staticmethod
    def _first_round_reason(made: list[ProbeRecord]) -> StopReason:
        if made and all(not p.consumable for p in made):
            return "insufficient_data"
        return "no_signal"

    def _run_round(self, round_no: int) -> list[ProbeRecord]:
        made: list[ProbeRecord] = []

        if round_no == 1:
            root = self._node(ROOT_ID)
            candidates = self._candidate_dims(root)
            dims = self._llm_pick_dims(candidates, 3)
            for dim in dims:
                if self.budget.probes_left() <= 0:
                    break
                made.append(self._run_probe(root, dim))

            # 首轮：按启发式评分保留 ≤2 分支
            good_probes = sorted(
                (p for p in made if p.consumable
                 and p.scores and p.scores.total >= EXPAND_THRESHOLD),
                key=lambda p: -p.scores.total,
            )[:2]
            for probe in good_probes:
                self._expand_child(root, probe)

            if not self._children(ROOT_ID):
                # 弱信号：首轮即提前停止
                root.state = "stopped"
                root.stop_reason = self._first_round_reason(made)
            return made

        # Round 2+：最高分 open 叶节点，只试探 1 个新维度
        leaves = sorted(
            self._open_leaves(),
            key=lambda n: -(n.scores.total if n.scores else 0.0),
        )
        if not leaves:
            return made
        node = leaves[0]
        candidates = self._candidate_dims(node)
        if not candidates:
            node.state = "stopped"
            node.stop_reason = "no_signal"
            self._persist()
            return made
        if self.budget.probes_left() <= 0:
            return made
        dim = self._llm_pick_dims(candidates, 1)[0]
        probe = self._run_probe(node, dim)
        made.append(probe)
        self._evaluate_after_probe(node, probe)
        return made

    # ------------------------------------------------ 主循环
    def run(self) -> DiagnosticSearch:
        while self.search.state == "running":
            if self.budget.rounds_left() <= 0 or self.budget.probes_left() <= 0:
                self._finish("budget_exhausted")
                break
            round_no = self.budget.used_rounds + 1
            made = self._run_round(round_no)
            self.budget.used_rounds += 1

            self.search.rounds.append(RoundRecord(
                round=round_no,
                action_summary=self._action_summary(round_no, made),
                probe_ids=[p.probe_id for p in made],
                next_action=self._next_action(),
            ))
            self._persist()

            if not self._open_leaves():
                root = self._node(ROOT_ID)
                # root 自身被停（弱信号/数据不足提前停）时保留具体原因
                reason = (
                    root.stop_reason
                    if root.state == "stopped" and root.stop_reason
                    else "root_completed"
                )
                self._finish(reason)
        return self.search

    def _finish(self, reason: StopReason) -> None:
        self.search.state = "completed"
        self.search.stop_reason = reason
        self.search.final_summary = self._final_synthesis()
        self._persist()

    def _action_summary(self, round_no: int, made: list[ProbeRecord]) -> str:
        if not made:
            return f"第 {round_no} 轮：无可试探维度或预算不足，未产生 probe。"
        return "；".join(
            f"第 {round_no} 轮试探「{p.dimension}」"
            f"（执行 {p.executed_count} / 失败 {p.failed_count}）"
            for p in made
        )

    def _next_action(self) -> str:
        leaves = self._open_leaves()
        if not leaves:
            return "无 open 分支，准备结束。"
        top = max(leaves, key=lambda n: n.scores.total if n.scores else 0.0)
        return f"下一轮在分支（depth={top.depth}）上选择新维度继续试探。"

    # ------------------------------------------------ 最终综合
    def _final_synthesis(self) -> str:
        probes = self.search.probes
        valid_ids = [p.probe_id for p in probes if p.consumable]
        payload = {
            "root_question": self.search.root_question,
            "stop_reason": self.search.stop_reason,
            "observations": {
                p.probe_id: {
                    "dimension": p.dimension,
                    "results": p.observations,
                } for p in probes
            },
            "output": '{"summary": "...", "evidence_probe_ids": [...]}',
        }
        try:
            result = generate_json(
                stage=_STAGE,
                fixture_name=f"synthesis_{self.sid[:8]}",
                system=_SYSTEM_SYNTHESIS,
                user=json.dumps(payload, ensure_ascii=False, default=str),
                schema=_Synthesis,
                business_validator=lambda o: (
                    [] if set(o.evidence_probe_ids) <= set(valid_ids)
                    else [f"引用了不存在/不可消费的探针：{set(o.evidence_probe_ids) - set(valid_ids)}"]
                ),
            )
            return result.summary
        except (FixtureMissingError, ContractError):
            return self._template_summary()

    def _template_summary(self) -> str:
        path_nodes = [n for n in self.search.nodes if n.parent_id is not None]
        path = " → ".join(
            n.scope_filters[-1].column
            + "="
            + (n.scope_filters[-1].values[0] if n.scope_filters[-1].values else "?")
            for n in path_nodes
        ) or "（首轮后即停止，未形成下钻路径）"
        best = [
            p for p in self.search.probes
            if p.consumable and p.scores and p.scores.total >= EXPAND_THRESHOLD
        ]
        best_txt = ""
        if best:
            top = max(best, key=lambda p: p.scores.total)
            best_txt = (
                f"最具区分度的维度是「{top.dimension}」"
                f"（启发式评分 {top.scores.total:.2f}，探针 {top.probe_id}）；"
            )
        return (
            f"诊断路径：{path}。{best_txt}"
            f"停止原因：{STOP_ZH.get(self.search.stop_reason, self.search.stop_reason)}。"
            "以上为会计拆解与启发式排序，不构成因果结论；"
            "可按探针中的分解数字逐项复核。"
        )


STOP_ZH: dict[str, str] = {
    "no_signal": "无显著信号，提前停止",
    "insufficient_data": "数据不足（零/单成员分组）",
    "insufficient_impact": "影响不足",
    "no_new_evidence_2x": "连续两次无新证据",
    "branch_failed_2x": "同分支连续失败",
    "budget_exhausted": "预算耗尽",
    "depth_exhausted": "已达最大下钻深度",
    "root_completed": "诊断正常收敛",
}


# ================================================================ 入口编排


def start_diagnostic_search(
    session_id: str,
    store: SessionStore,
    *,
    root_question: str | None = None,
) -> DiagnosticSearch:
    """新建或续跑当前运行上的诊断搜索。

    - 无 search：新建（绑定 active run）；
    - running：续跑（预算不重置、不重复已执行 probe）；
    - completed：原样返回；
    search.run_id ≠ active run 时抛 StaleRunError（路由映射 409）。
    """
    active_rid = store.active_run_id(session_id)
    if active_rid is None:
        raise StorageError("尚未生成分析蓝图（AnalysisBundle）。")

    if store.has_diagnostic(session_id):
        existing = DiagnosticSearch.model_validate(
            store.read_diagnostic(session_id)
        )
        if existing.run_id != active_rid:
            raise StaleRunError(
                "诊断搜索属于旧运行版本，请在当前运行上重新发起。"
            )
        if existing.state == "completed":
            return existing
        return _build_controller(session_id, store, existing).run()

    manifest = store.run_manifest(session_id)
    bundle = AnalysisBundle.model_validate(store.read_bundle(session_id))
    dictionary = DataDictionary.model_validate(
        store.read_artifact(session_id, "dictionary")
    )
    snapshot = store.load_snapshot(session_id)
    norm = normalize_scope(snapshot, manifest.scope)
    scope_snapshot = apply_scope_filters(snapshot, norm)

    metric = bundle.primary_metrics[0] if bundle.primary_metrics else ""
    date_col = dictionary.date_fields()[0] if dictionary.date_fields() else None
    field = next((f for f in dictionary.fields if f.name == metric), None)
    func = _spec_func(effective_metric_spec(field)) if field else "sum"
    func = getattr(func, "value", func) or "sum"

    period = "mom"
    period_spec: dict[str, Any] = {}
    question = root_question or _default_question(
        scope_snapshot, metric, func, date_col, period
    )
    if date_col:
        try:
            from app.services.period_compare import build_period_spec
            dates = pd.to_datetime(
                scope_snapshot[date_col], errors="coerce"
            ).dropna()
            if len(dates):
                period_spec = build_period_spec(
                    dates.max(), period, dates
                ).model_dump(mode="json")
        except Exception:  # noqa: BLE001
            period_spec = {}

    search = DiagnosticSearch(
        search_id=f"search_{uuid.uuid4().hex[:10]}",
        run_id=active_rid,
        root_question=question,
        period=period,
        period_spec=period_spec,
        root_scope=[
            ScopeFilter(column=str(f.get("column", "")),
                        values=[str(v) for v in f.get("values", [])])
            for f in manifest.scope
            if str(f.get("column", ""))
        ],
        budget=SearchBudget(),
        nodes=[SearchNode(node_id=ROOT_ID, depth=0)],
        state="running",
        created_at=_now(),
        updated_at=_now(),
    )
    store.write_diagnostic(session_id, search.model_dump(mode="json"))
    return _build_controller(session_id, store, search).run()


def _default_question(
    df: pd.DataFrame, metric: str, func: str,
    date_col: str | None, period: str,
) -> str:
    if date_col and metric:
        try:
            from app.services.period_compare import period_comparison
            pc = period_comparison(df, date_col, metric, func, period)
            if pc is not None and pc.get("growth_pct") is not None:
                if pc["growth_pct"] < 0:
                    return (
                        f"为什么「{metric}」环比下降 "
                        f"{abs(pc['growth_pct']):.1f}%？"
                    )
                return f"「{metric}」的表现由哪些维度驱动？"
        except Exception:  # noqa: BLE001
            pass
    return f"「{metric or '核心指标'}」的表现由哪些维度驱动？"


def _build_controller(
    session_id: str, store: SessionStore, search: DiagnosticSearch
) -> DiagnosticController:
    manifest = store.run_manifest(session_id, search.run_id)
    bundle = AnalysisBundle.model_validate(
        store.read_bundle(session_id, search.run_id)
    )
    dictionary = DataDictionary.model_validate(
        store.read_artifact(session_id, "dictionary")
    )
    snapshot = store.load_snapshot(session_id)
    norm = normalize_scope(snapshot, manifest.scope)
    scope_snapshot = apply_scope_filters(snapshot, norm)

    metric = bundle.primary_metrics[0] if bundle.primary_metrics else ""
    date_col = dictionary.date_fields()[0] if dictionary.date_fields() else None
    field = next((f for f in dictionary.fields if f.name == metric), None)
    func = _spec_func(effective_metric_spec(field)) if field else "sum"
    func = getattr(func, "value", func) or "sum"

    return DiagnosticController(
        session_id, store, search,
        dictionary=dictionary, scope_snapshot=scope_snapshot,
        metric=metric, func=func, date_col=date_col, bundle=bundle,
    )
