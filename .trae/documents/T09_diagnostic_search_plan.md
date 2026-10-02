# T09 预算受控的 Diagnostic Search 实施计划

## Repository Research

现有可复用机制（均已核实）：

- **运行版本化**（T01）：[storage.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/services/storage.py) 以 `bundle/runs/{run_id}/` 隔离全部产物，`active/current` 原子指针；已预留 `probes/` 目录。
- **执行内核**：`execute_plan(snapshot, plan)`（[executor.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/services/executor.py)）→ `(df, records, elapsed_ms)`，末端 `records[-1].summary` 即算子摘要；View 独立失败、异常收敛模式已在 [bundle_executor.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/services/bundle_executor.py#L145-L215) 建立范式。
- **消费门禁**（T04）：`ViewExecutionResult.consumable`；零行=no_data，绝不以 0 冒充。
- **分解算子**（T08）：`contribution`（total_delta/unexplained/identity_residual）、`rate_decomposition`（within/mix/interaction/change_pp）已注册于 catalog。
- **候选与口径**（T03/T07）：`effective_metric_spec`、`_ranked_dimensions`、`_spec_func`、`detect_derived_metrics`；维度基数、MetricSpec.additive/entity_key 可直接取用。
- **LLM 契约**：`generate_json(..., schema, business_validator)`（[llm/client.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/services/llm/client.py)）强制 JSON + 有限修复；`FixtureMissingError` 时走规则降级（同 followup 模式）。
- **方案构造**：`offline_fallback.build_plan(question, op, hint, fields)` 可造单步 plan；多步（filter 链 + terminal）需新封装。

关键约束：pandas 确定性引擎；LLM 不产数字/代码/公式；全部 schema `extra="forbid"`；probe/视图失败不拖垮整体。

## 设计总览

一个**单控制器**在当前 run 内做深度受限、预算严格封顶的自适应维度诊断：

```
root（根问题 + 继承 run 的 PeriodSpec / scope filters）
 ├ Round1: ≤3 个候选维度各 1 probe → 评分 → 保留 ≤2 分支（depth=1，scope 各加 1 个成员 filter）
 ├ Round2+: 只展开当前最高分叶节点上的 1 个新维度 probe → 显著则下钻（depth+1），否则停该分支
 └ 深度 ≤3；每轮有动作摘要/observation/下一步/stop_reason
```

预算硬边界（全部计数持久化，失败也耗预算，恢复/重试不重置）：

| 预算 | 上限 |
|---|---|
| rounds（LLM 选择次数同此） | 4 |
| probes（每维试探=1 probe） | 8 |
| depth（维度过滤路径） | 3 |
| terminal plans / probe | 3 |
| 额外 terminal 执行总数 | 24（=8×3，天然封顶） |

## Files and Modules

- **新建 `backend/app/schemas/diagnostic.py`**：Search 全部契约（budget/node/probe/round/search）。
- **新建 `backend/app/services/diagnostic_search.py`**：控制器、probe plan 构造、启发式评分、分支选择、停止判定、LLM 编排与规则降级、中断恢复。
- **改 `backend/app/services/storage.py`**：新增 search 状态与 search 专属 probe 产物的读写（独立目录，不与 dashboard 的 ≤3 probe 混用）。
- **新建 `backend/app/routers/diagnostic.py`**：启动/续跑 + 读取状态；在 main.py 注册。
- **新建 `backend/tests/test_t09_diagnostic_search.py`**：预算封顶/提前停/失败耗预算/中断恢复/高基数惩罚/降级路径/黄金案例。
- 前端不改（T11 做主工作区）；search 经 API 即可被验证与演示。

## 契约设计（schemas/diagnostic.py，全部 extra="forbid"）

- `SearchBudget`：`max_rounds=4, max_probes=8, max_depth=3, max_plans_per_probe=3`；
  另存 `used_rounds/used_probes/used_terminal_executions`（计数只从持久化状态恢复）。
- `NodeState = open|expanded|stopped`；`StopReason` 枚举：
  `no_signal / insufficient_data / insufficient_impact / no_new_evidence_2x / branch_failed_2x / budget_exhausted / depth_exhausted / root_completed`。
- `SearchNode`：`node_id, parent_id, depth, scope_filters（继承链，有序）, dimension, state, stop_reason, score_breakdown`。
- `ProbeScores`：`concentration, impact, support, stability, novelty, cardinality_penalty, total`；
  字段注释明确「启发式排序分，非因果解释度、非统计信息增益」。
- `ProbeRecord`：`probe_id, node_id, round, dimension, scope_filters, plans（≤3 的 op+params 摘要）, executed_count, failed_count, consumable, observations（结构化键值），scores, failed_reason`。
- `RoundRecord`：`round, action_summary（可核实短句）, probe_ids, next_action`。
- `DiagnosticSearch`：`search_id, run_id, root_question, period_spec, root_scope, budget, nodes, probes, rounds, state(running|completed), stop_reason, final_summary`。

## 实施步骤（依赖顺序）

1. **storage 扩展**：
   - `diagnostic_dir(sid)` = `active_run_dir/diagnostic/`；search.json 原子写；
   - search 专属 probe 目录 `diagnostic/probes/{probe_id}/`（plan/result/ledger），与 dashboard `probes/` 完全隔离；
   - `read/write_diagnostic`、`save_diagnostic_probe_*`；run 不一致由调用方判 stale。
2. **契约**：按上节落 `schemas/diagnostic.py`，导入自检。
3. **Probe plan 构造**（diagnostic_search.py）：
   - `_scope_filter_steps(node)`：把继承链（≤3 个维度成员）转为有序 `FilterStep(op="filter", operator=in_set/==)`；
   - `_terminal_plans(node, dim)`：按可用字段造 **1–3** 个 terminal，不凑数：
     1. `group_by(dim, m0)` → 当前分组值/集中度/support；
     2. `contribution(dim, m0)`（可加/合格口径）或 `compare_groups(dim)` → 变化影响；
     3. 有双字段率时 `derive_ratio(dim)` / `rate_decomposition(dim)`；
   - 计划即校验：拒绝空分组、单成员分组、重复 scope（相同 filter 集合+dim）、不适用字段/指标。
4. **启发式评分**（确定性，数值只来自引擎结果）：
   - concentration：分组值 HHI/top-1 占比（0–1）；
   - impact：最大组间差或 |total_delta| 相对基期标准化（0–1）；
   - support：最小组行数/独立实体数，对最小阈值（每组 ≥5 行；有 entity_key 时 ≥3 实体）衰减；
   - stability：基期→当前权重变化（区分"一直如此"与"结构变化"）；
   - novelty：对已展开分支与 bundle 内现有视图重叠度取反；
   - cardinality_penalty：card>30 线性惩罚；
   - `total = 0.30·impact + 0.20·concentration + 0.15·stability + 0.15·support + 0.20·novelty − penalty`；
     低于展开阈值（0.25）不展开。
5. **控制器 `DiagnosticController`**：
   - 构造时从 search.json 恢复全部计数（恢复不重置）；每个 probe 后立即原子落盘；
   - `run_probe(node, dim)`：预算门禁 → 造 plans → 逐个 `execute_plan(scope_snapshot, plan)`，
     **成功/失败都计入 used_terminal_executions**；异常收敛为 probe 失败；
   - Round1：选 ≤3 候选维度（低中基数优先，Discount 固定分箱，高基数带惩罚与最小实体数，时间维度需可比周期），≤3 probes，保留 ≤2 分支；
   - Round2+：只在最高分 open 叶节点选 1 个未试探维度；评分显著→展开（depth+1，scope 加成员 filter），否则停该分支；
   - 每轮产出 `RoundRecord`（动作摘要/observation/next_action），不输出内部思维；
   - 停止：无信号/数据不足/影响不足/连续两次无新证据/同分支连续失败/预算·深度耗尽；
   - 正常收敛或预算耗尽时出 `final_summary`（只引用真实 probe_id 与其数字键，失败不伪造结论）。
6. **LLM 编排与降级**：
   - 每轮分支选择走 LLM（≤4 次）：输入候选节点+评分（不含内部思维），输出过契约，业务校验只能选 open 且存在的节点；
   - 最终综合 1 次：业务校验所有引用必须指向真实 probe/数字；
   - `FixtureMissingError`/无 key → 规则降级：取 total 最高分分支 + 模板综合（必须可演示）；
   - LLM 调用本身失败按"无新证据/失败"计预算并可降级，不无限重试。
7. **根问题与范围继承**：
   - 默认 root：run 主指标环比下滑 → "为什么 {m0} 下降？"，否则 "{m0} 表现由哪些维度驱动？"；允许 body 覆盖；
   - 继承当前 run 的 PeriodSpec（mom）与 manifest.scope；统一在 `scope_snapshot`（已按 manifest scope 裁剪）上执行，保证 probe 间口径一致。
8. **路由**（routers/diagnostic.py，前缀 `/sessions/{id}`）：
   - `POST /diagnostic-search`：无 search→新建；running→**续跑**（预算不重置）；completed→原样返回；
     search.run_id ≠ active run → 409；
   - `GET /diagnostic-search`：读状态（含预算用量与路径）；
   - main.py 注册。
9. **演示脚本**（可选，`scripts/run_diagnostic_demo.py`）：对 sales_orders 跑全流程并打印预算/路径/停止原因，验证规则降级。

## Dependencies and Considerations

- search probe 与 dashboard ProbeRunner（MAX_PROBES=3）**目录与计数双隔离**，避免互相挤占或污染。
- terminal 执行数 24 由「8 probes × 3 plans」数学封顶，另设独立计数器做显式断言。
- 评分纯启发式：文案与字段注释均不得出现"因果/信息增益/显著性检验"字样。
- 不新增算子、不改 plan 白名单；filter 链严格复用现有 FilterStep（教训：必须 `op="filter"`）。
- 时间维度不作 filter 路径成员；时间比较仅通过 terminal 算子的可比 PeriodSpec。

## Validation

- **新建 test_t09_diagnostic_search.py**，覆盖：
  1. 预算严格封顶：构造多维数据，断言 probes≤8、terminal_executions≤24、rounds≤4、depth≤3；
  2. "扫描几十维算一次 probe" 不存在：每维试探恰为 1 probe，计数可逐一对账；
  3. 弱信号提前停：稳定/均匀分布数据 → Round1 后 `no_signal` 停止，不继续耗预算；
  4. 失败耗预算：注入不适用字段/引擎拒绝 → used 计数增加且不伪造结论；
  5. 中断恢复：模拟 controller 中途结束后再次 POST → 不重复已执行 probe、不超额；
  6. 高基数惩罚 + 最小独立实体数：Customer/Product 高基数被惩罚/拒绝；
  7. 规则降级路径：无 LLM 时确定性可选可跑通且输出过契约；
  8. 黄金案例：构造"某维度贡献集中"数据，search 在预算内定位到正确维度并产出可回算 observation。
- **全量回归**：`cd backend && uv run pytest`（基线 229 passed）；导入自检
  `python -c "from app.services import diagnostic_search; from app.schemas import diagnostic"`。
- 前端无改动，无需 typecheck/build；重启后端加载新路由。

## Risks

- **预算计数漂移/恢复重复**：所有计数只以持久化 search.json 为唯一事实，每个 probe 原子落盘，
  恢复时先按 probe_id 去重；测试专门覆盖中断续跑。
- **Probe plan 过多导致变相全表扫描**：plan 数硬封顶 3、维度首轮 ≤3，且仅在单一 scope_snapshot 上执行；
  加显式断言。
- **LLM 输出引用幻觉**：结构 + 业务双校验（引用必须命中真实 probe/数字键），失败即修复→降级，不直接展示。
- **旧测试受影响**：search 走独立目录，理论上零影响；若全量回归出现漂移按实际隔离修正。
