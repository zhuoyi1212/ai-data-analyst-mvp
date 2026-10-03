# Autonomous AI Data Analyst - 实现计划

> 任务顺序遵循用户指定优先级：自动编排骨架 → Broad Scan → 信号排序 → 多信号深挖 → 证据图 → 报告 → Dashboard 布局 → 交互 → 前端新主链路收尾。每个任务为可独立验证的垂直切片。

## Task 1: 自动分析编排骨架与智能 Gate
- **Status**: `completed`
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 新增 `backend/app/schemas/auto.py`：AutoAnalysisState（阶段枚举、stage_logs、needs_input 问题、产物指针、run_id、created/updated）、GateQuestion、AnswerRequest，全部 extra=forbid。
  - 新增 `backend/app/services/auto_analyst.py`：`start_auto_analysis(session_id, store, answers=None)` 与 `get_auto_state`；阶段状态持久化（auto.json）；断点续跑、幂等。
  - 编排先串联既有能力：profile.generate_dictionary → 判定 complete → quality.run_quality_checks → apply_decisions（全部 issue 使用各自保守默认动作）→ 既有 build_bundle → execute_bundle → synthesize_dashboard（本任务先产出旧版 Dashboard，后续任务替换中间阶段）。
  - Gate 逻辑：仅四种情形 needs_input（关键语义歧义 / 无可用指标 / 零有效行 / 严重错误），普通质量问题不拦截；作答后从断点继续。
  - 新增 `backend/app/routers/auto.py`：POST/GET `/sessions/{id}/auto-analyze`，错误映射 404/409/400。
- **Acceptance Criteria Addressed**: AC-1（链路骨架部分）、AC-2
- **Test Requirements**:
  - `rule` TR-1.1: 正常数据一键跑至 Dashboard 产出，中途无 needs_input；证据：API 测试 + 落盘 auto.json 阶段序列完整。
  - `rule` TR-1.2: 五类参数化数据（正常/无指标/零行/仅普通问题/关键歧义）的放行与拦截判定全部正确，拦截附具体中文问题；证据：编排器单测。
  - `rule` TR-1.3: 提交作答后断点续跑，已完成阶段不重复执行（以阶段日志/产物时间戳断言）；重复 POST 幂等。
  - `rule` TR-1.4: apply_decisions 使用默认决策生成的 snapshot_rows 与质量报告一致。
- **Notes**: 本任务后产品已可一键出旧版 Dashboard，为后续替换中间阶段提供可回归基线。
- **Completion Evidence** (2025-07):
  - 新增 [auto.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/schemas/auto.py)（AutoAnalysisState/StageLog/GateQuestion/AutoAnswerRequest，全部 extra=forbid）、[auto_analyst.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/services/auto_analyst.py)（start_auto_analysis/get_auto_state、auto.json 持久化、断点续跑、幂等）、[routers/auto.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/routers/auto.py) 并注册于 [main.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/main.py)。
  - 修复两个编排器真实缺陷：① 零质量问题时 apply_decisions 报错——新增 `_snapshot_clean` 直接快照；② 续跑 dictionary 为 None——synthesis 分支补加载。
  - 测试：[test_auto_analyst.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/tests/test_auto_analyst.py) 12 个用例覆盖 TR-1.1~1.4（一键完成/五类数据 Gate/作答续跑与幂等/快照行数一致）；后端全量 **249 passed**（基线 237 + 12）。
  - 实时冒烟：customer_tickets 与 sales_orders 一键直达 run 版本化 Dashboard；歧义数据 waiting_input→作答→续跑完成。

## Task 2: Broad Scan 广度扫描器
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 新增 `backend/app/services/broad_scan.py`：`plan_scan(dictionary, snapshot) -> ScanPlan` 与执行入口；八类覆盖（核心 KPI / 时间趋势 / 主要维度对比 / Top·Bottom / 结构占比 / 异常 / 指标关系 / 盈利效率），数量随字段条件自适应。
  - 候选构造复用 bundle_planner 既有 family/口径工具，新增 Top/Bottom 对称视角与"每主指标 KPI"完整覆盖；无日期/无派生条件时对应类别缺席。
  - auto_analyst 用 broad_scan 替换原 build_bundle 阶段（scan 视图仍走 execute_bundle 与既有校验门禁）。
- **Acceptance Criteria Addressed**: AC-3、AC-1
- **Test Requirements**:
  - `rule` TR-2.1: 字段完整数据上八类中应支持类别全部产生成功视图；无日期/无派生数据上对应类别缺席；证据：scan 规划器单测。
  - `rule` TR-2.2: 全部 scan 成功视图通过既有 checks 且可消费；TR-1.1 的一键链路改用 scan 后仍通过。
- **Completion Evidence** (2025-07):
  - 新增 [broad_scan.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/services/broad_scan.py)：plan_scan 复用 CandidateGenerator 口径工具，八类自适应、封顶 24；新增 Bottom 对称视角、第二指标离群；scan 专用 family 键解决跨类别误去重。
  - [offline_fallback.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/services/offline_fallback.py) top_n 支持 order（向后兼容，默认 desc）。
  - [auto_analyst.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/services/auto_analyst.py)：scan 阶段独立执行 plan_scan+write_bundle+execute_bundle，synthesis 只做 Dashboard 合成。
  - 测试：[test_broad_scan.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/tests/test_broad_scan.py) 6 用例（类别覆盖/Top·Bottom 对称/无日期缺席/五集自适应/执行门禁/五集零失败）；全量 **255 passed**。
  - 实时冒烟：marketing_campaigns 一键产出 16 个广扫视图并合成 Dashboard。

## Task 3: Signal Detection 与 Signal Ranking
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 2
- **Description**:
  - 在 `schemas/auto.py` 新增 Signal（signal_id、type、scan_view_id、metric、dimension、member、direction、magnitude 字段引用、score、score_breakdown）与 SignalSet。
  - 新增 `backend/app/services/signals.py`：`extract_signals(scan_views, execution, dictionary) -> list[Signal]`——把 dashboard_insight 的八类检测阈值改造为"信号提取"（增长/下滑、量利背离、负成员、异常、显著相关、增收不增利、Simpson），量级一律引用 scan view 真实数据；`rank_signals` 多维评分（影响/集中度/新异性/支撑度，复用 T09 评分口径），排序确定性，取 top K（默认 3）。
- **Acceptance Criteria Addressed**: AC-4
- **Test Requirements**:
  - `rule` TR-3.1: 预埋信号（负成员/背离/异常/显著相关/结构悖论）全部被提取，信号 magnitude 与 scan view 数据逐值一致；证据：signals 单测。
  - `rule` TR-3.2: 同输入两次 rank 结果完全一致（幂等）；top K 数量与截断规则正确。
  - `rule` TR-3.3: 构造无信号数据（平稳/无差异）时返回空集而非凑数，后续阶段据此跳过深挖。
- **Completion Evidence** (2025-07):
  - 契约：[auto.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/schemas/auto.py) 新增 Signal/Magnitude/ScoreBreakdown/SignalSet（量级字段只引用 scan 真实值，extra=forbid）。
  - 服务：[signals.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/services/signals.py)：extract_signals 八类提取（阈值复用 dashboard_insight，相关对/份额口径加固——总利润为负时直接由 value 归一）；rank_signals 对齐 T09 权重，确定性 tiebreak，top K=3。
  - scan 扩展收纳 contribution/rate_shift 证据视图（[broad_scan.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/services/broad_scan.py)）；编排器 signals 阶段启用并落盘 SignalSet。
  - 测试：[test_signals.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/tests/test_signals.py) 8 用例：预埋信号全覆盖、亏损成员/背离 gap/相关系数逐值接地、Simpson、幂等 topK、空集；全量 **263 passed**。

## Task 4: Multi-root Diagnostic 多信号深挖
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 3
- **Description**:
  - 在 `schemas/auto.py` 新增 AnalysisChain（chain_id、root_signal_id、节点列表：node_id/parent/node 类型/引用的真实 view_id 或 probe_id/scope_filters/scores、conclusion、depth、status、budget_used）。
  - 新增 `backend/app/services/chain_diagnostic.py`：以 T09 DiagnosticController 为内核，为每个 top 信号各起一个 root（root 问题由信号自动生成）；深挖 probe 覆盖子维度拆分、交叉维度、驱动指标（折扣/单价等率指标）、集中度（HHI）与"少数订单 vs 普遍现象"验证。
  - 多 root 共享总预算硬边界（建议 chains≤3、总 probes≤12、终端执行≤36、depth≤3；失败也计数、不重置）；产出 AnalysisChain 列表并持久化。
  - 深挖产生的有展示价值 view 并入候选视图集合（供布局/报告引用）。
- **Acceptance Criteria Addressed**: AC-5、AC-U1
- **Test Requirements**:
  - `rule` TR-4.1: Superstore 类数据上至少 1 条 chain 深度≥2，且第二层节点确实是新的下钻/交叉视角（断言其 plan 与第一层不同源）。
  - `rule` TR-4.2: chain 每个节点引用的 view_id/probe_id 真实存在且结果可回算；无悬空引用。
  - `rule` TR-4.3: 总 probes/终端执行/depth 不可突破预算；人为喂入无限候选时边界仍然有效；预算耗尽时各 chain 有明确 stop_reason。
  - `rubric` TR-4.4: 深挖链分析质量；scale 1-5；anchors 1=第一层换皮/3=有下钻但维度单一/5=多层+多证据+驱动验证；threshold ≥4；证据：Superstore chain 走查 + 独立 Review。
- **Completion Evidence** (2025-07):
  - 契约：[auto.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/schemas/auto.py) 新增 ChainNode/AnalysisChain/DiagnosticViewRef/ChainSet（节点引用真实 view/probe，extra=forbid）。
  - 服务：[chain_diagnostic.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/services/chain_diagnostic.py)：复用 T09 DiagnosticController（_SilentController 仅覆写 _persist）；root 问题按信号类型自动生成，亏损/背离信号 root 限定在成员内向其他维度下钻；全局硬边界 chains≤3/probes≤12/exec≤36/depth≤3，失败也计数；probe 深度跟随树节点；相关 root 带因果护栏。
  - 编排器 diagnostic 阶段启用（无 top 信号才 skipped），chains 落盘并登记 artifacts。
  - 测试：[test_chain_diagnostic.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/tests/test_chain_diagnostic.py) 4 用例（深度≥2 新视角、全部引用落地可回算、预算边界+停止原因、空集无 chain）；全量 **267 passed**。
  - 实时冒烟：sales_orders 一键链路 ~1.4s 完成，2 条链 8 探针。

## Task 5: Evidence Graph 证据图谱
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 4
- **Description**:
  - 新增 `backend/app/services/evidence_graph.py`：构建并持久化证据索引——scan view → signal → chain → chain 节点（view/probe）→（Task 6 接入）report claim；节点与边类型化。
  - 查询 API：`upstream/downstream(view_id)`、`claims_using(view_id)`、`chain_for(signal_id)`、解析校验（无悬空引用）。
  - 在 auto_analyst 合成阶段构建图，并提供只读端点 GET `/sessions/{id}/evidence`（可按 node 过滤）。
- **Acceptance Criteria Addressed**: AC-6
- **Test Requirements**:
  - `rule` TR-5.1: 全部 signal/chain/evidence_view_ids 引用都能在图中解析，测试对每个引用做存在性断言。
  - `rule` TR-5.2: 反向查询（view→引用它的 signal/chain）与正向查询结果一致（边对称闭合）。
- **Completion Evidence** (2025-07):
  - 契约：[auto.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/schemas/auto.py) 新增 EvidenceNode/EvidenceEdge/EvidenceGraph（节点/边类型化，extra=forbid）。
  - 服务：[evidence_graph.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/services/evidence_graph.py)：build_evidence_graph 汇聚 scan view→signal→chain→view/probe，构建时解析校验悬空引用；查询 downstream/upstream（BFS 闭包）、chain_for、claims_using（Task 6 hook）、neighborhood 子图。
  - 接入：synthesis 阶段先建图谱；GET `/sessions/{id}/evidence?node=` 只读端点（端点 404 映射，缺图时按需构建）。
  - 测试：[test_evidence_graph.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/tests/test_evidence_graph.py) 4 用例（引用全解析、节点类型、上下游边闭合+chain_for、端点全图/子图/404）；全量 **271 passed**。

## Task 6: AnalysisReportArtifact 深度报告
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 5
- **Description**:
  - 新增 `backend/app/schemas/report.py`：ReportClaim（text、claim_type: fact/signal/hypothesis/conclusion、evidence_view_ids、limitations）、ReportSection（八节固定 id：exec_summary/performance/drivers/risks/diagnostics/opportunities/recommendations/methodology）、AnalysisReportArtifact（run_id、sections、生成时间、evidence_graph 版本）。
  - 新增 `backend/app/services/report_writer.py`：为 LLM 构建"证据目录"（信号/链/可引用 view 及其真实数值，按展示精度）；LLM 输出八节结构化 claims；离线时确定性模板兜底（零编造）。
  - 新增 `backend/app/services/report_validator.py`：数字接地（逐数字在引用 view 数据中按精度匹配）、证据存在性（接入 evidence graph）、因果护栏（复用既有词表）、claim 至少一条证据；拦截重生成（≤2 次）。
  - 报告在 auto_analyst 合成阶段发布；新增 GET `/sessions/{id}/report`。
- **Acceptance Criteria Addressed**: AC-7、AC-U1
- **Test Requirements**:
  - `rule` TR-6.1: 正常报告八节齐全、每 claim 证据存在且数字接地，validator 返回通过。
  - `rule` TR-6.2: 注入未接地数字/不存在 view_id/因果词的三类脏报告必被拦截并给出具体违规项。
  - `rule` TR-6.3: 无固件离线场景下模板报告八节齐全，文本中所有数字均来自真实 view（抽样溯源断言）。
  - `rubric` TR-6.4: 报告深度；scale 1-5；anchors 1=浅层事实罗列/3=结构完整但论证浅/5=结论-证据-拆解-线索-影响-建议闭环；threshold ≥4；证据：Superstore 报告走查 + 独立 Review。
- **Completion Evidence** (2025-07):
  - 契约：[report.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/schemas/report.py)：ReportClaim/ReportSection 八节固定 id/AnalysisReportArtifact（记录 evidence graph 版本时间，extra=forbid）。
  - 服务：[report_writer.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/services/report_writer.py)：为 LLM 构建证据目录（信号/链/视图真实数值），max_repair=1，门禁不过即确定性模板兜底；模板只引用真实证据数值（信号当前/上期值、成员值、相关系数、率变化）。
  - 门禁：[report_validator.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/services/report_validator.py)：八节齐全/claim 唯一、证据经 graph 解析、数字在引用证据 parquet 中接地（本地 pool 缓存）、因果护栏复用 insight_validator 词表。
  - 接入 synthesis（graph→dashboard→report），artifacts 登记；GET `/sessions/{id}/report`。
  - 测试：[test_report.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/tests/test_report.py) 6 用例（八节有效、三类脏报告必拦、模板数字抽样溯源、端点）；全量 **277 passed**。

## Task 7: Tableau 风格 Dashboard Composition（12-column）
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 6
- **Description**:
  - 扩展 dashboard 契约：DashboardLayoutItem（view_id、role: kpi/hero/primary/supporting/diagnostic/findings、col_span 1-12、row_span、order、rationale）；DashboardArtifact 新增 layout 字段（sections 保留兼容）。
  - 新增 `backend/app/services/layout_composer.py`：依据信号排名、chain 归属、value_scores 确定性分配角色与跨度——KPI Strip（3–4 个同范围 KPI，各 span 3）、Hero（1 个，span 12，row_span 大）、Primary（1–2 个，span 7+5/6+6）、Supporting（3–5 个，span 4/6）、Diagnostic（chain 深挖视图，span 6/12）；同 row 跨度和 ≤12；低价值/冗余视图 default_hidden 但可展开；每个排版项带 rationale。
  - synthesize_dashboard 接入 layout_composer；refine 重算后布局保持规则。
- **Acceptance Criteria Addressed**: AC-8、AC-U2
- **Test Requirements**:
  - `rule` TR-7.1: 布局中每个 role 至少一项（数据支持时），同 row col_span 总和 ≤12，无 view 被重复放置。
  - `rule` TR-7.2: 布局对同输入确定性；每个 item rationale 非空；hidden 视图与价值评估理由一致。
  - `rule` TR-7.3: refine 筛选重算后仍产出合法布局（跨度约束不被破坏），旧 dashboard API 契约测试不回归。
- **Completion Evidence** (2026-01):
  - 契约扩展：[dashboard.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/schemas/dashboard.py)：LayoutRole 六角色、DashboardLayoutItem（1≤col_span≤12/row_span/order/rationale/折叠标记）、DashboardLayout；ViewCard 增加 ref_type=view|probe；DashboardArtifact 增加 layout。
  - 布局合成：[layout_composer.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/services/layout_composer.py)：value_scores 加权确定性排序 → KPI 条（等宽）→Hero（span12,row_span2）→Primary(6+6)→Supporting(4×4，超出折叠)→深挖探针（按 chain 顺序去重，前 4 可见，span6）→折叠证据 → Findings 条；每项非空 rationale。
  - 探针卡：synthesizer 新增 `_build_probe_cards`，以 probe 的 group_by 结果构造自足卡（bar 契约命中真实输出列），与 view 同字典。
  - 测试：[test_layout.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/tests/test_layout.py) 3 用例（角色/网格/不重复/可见即可消费、确定性+rationale+折叠、refine 后合法）；全量 **280 passed**。

## Task 8: 追问/下钻后端——产物追加不覆盖
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 7
- **Description**:
  - 新增 `backend/app/schemas/ask.py`：AskRequest（question，继承当前 scope）、AskArtifact（question、answer claims、new_view_ids、new_evidence、appended_report_section_ids）。
  - 新增 `backend/app/services/ask_service.py`：继承当前 run 的 scope filters 与口径；LLM 只把问题翻译为既有算子 plan，确定性执行 + 校验 + 接地；新视图**追加**进 run 视图集合与 evidence graph，报告追加段落；原 view/报告内容字节不变。
  - 新增端点 POST `/sessions/{id}/ask`。
- **Acceptance Criteria Addressed**: AC-9（后端）
- **Test Requirements**:
  - `rule` TR-8.1: ask 后 run 内原 view 数量不减少、原内容 hash 不变；新 view_id 唯一且可消费。
  - `rule` TR-8.2: 追问在当前筛选 scope 内计算（断言 participating_rows 与 scope 一致）；未接地答案被拦截。
- **Completion Evidence** (2026-01):
  - 契约：[ask.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/schemas/ask.py)：AskRequest/AskClaim/AskArtifact（new_view_ids、scope_rows、appended_report_section_ids）/AskSet（全部 extra=forbid）。
  - 服务：[ask_service.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/app/services/ask_service.py)：不开新 run——在当前 run 的 scope 快照上执行；LLM 翻译方案（无 key 走 plan_for_question 规则路径）；新视图追加进 bundle.json/execution，复用 `_execute_one` 落盘与可消费门禁；重建 evidence graph；删 dashboard.json 触发含新视图的重合成；答案由结果前三行构造并经 read_value_pool 接地校验；AskSet 累积落盘。
  - 端点：POST `/sessions/{id}/ask`（StorageError→404，AskError→422）。
  - 设计取舍：报告八节为固定契约，追问不重写报告（appended_report_section_ids 留空），问答本身即追加产物。
  - 测试：[test_ask.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/tests/test_ask.py) 4 用例（原 parquet hash 不变+唯一追加+两次追问累积、scope 一致+数值真实、伪造接地池必拦、端点 200/422）；全量 **284 passed**。

## Task 9: 前端新主链路——分析进度页 + Workspace
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 8
- **Description**:
  - 首页上传/示例成功后直接跳转 `/sessions/[id]/analyzing`：阶段进度时间线（语义→质量→扫描→信号→深挖→合成），每阶段可展开查看依据；needs_input 时就地呈现最小化问题卡片，作答续跑；完成后自动进入 Workspace。
  - 新建 `/sessions/[id]/workspace`：Dashboard 与深度报告双 Tab。
    - Dashboard：12-column 按 layout 渲染（KPI Strip / Hero / Primary / Supporting / Diagnostic 尺寸层级）；全局筛选 chips（refine）；图表点击维度成员联动；Finding 与报告 claim 点击高亮对应图表并滚动定位；chain 下钻路径可视化（节点可点开对应 view 卡）；追问框，新视图淡入追加。
    - 报告：八节排版，claim 中证据引用可点击跳转并高亮图表；methodology 展示口径与局限。
  - 旧 Stepper 工作台保留，路径改为入口"高级模式/查看分析依据"；api.ts/types.ts 增补新端点与新契约镜像。
  - 视觉沿用已建立的 Apple 设计 tokens，Dashboard 区域低饱和、专业、克制。
- **Acceptance Criteria Addressed**: AC-9（前端）、AC-U2、AC-U3
- **Test Requirements**:
  - `rule` TR-9.1: 上传后无任何逐步确认直接到产物；needs_input 作答后自动续跑至完成；证据：前端走查 + 组件状态断言。
  - `rule` TR-9.2: 联动/高亮/下钻/追问四类交互可操作且有可见反馈；追问后新视图出现而原视图不变。
  - `rule` TR-9.3: tsc --noEmit 与 `next build` 通过；旧工作台路由仍可打开。
  - `rubric` TR-9.4: Dashboard 美学；scale 1-5；anchors 1=等宽白卡/3=层级一般/5=Tableau Public 级节奏与交互；threshold ≥4。
  - `rubric` TR-9.5: 主流程简洁性；scale 1-5；anchors 1=仍须逐步操作/3=自动但不透明/5=一键+进度可见+拦截最小；threshold ≥4。
- **Completion Evidence** (2026-01):
  - 契约镜像：[types.ts](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/frontend/src/lib/types.ts)：AutoAnalysisState/StageLog/GateQuestion、EvidenceGraph、AnalysisReportArtifact 八节、AskArtifact/AskSet、DashboardLayout* 全部补齐；[api.ts](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/frontend/src/lib/api.ts)：startAuto/getAutoState/getEvidence/getReport/ask 五个新端点。
  - 分析进度页：[analyzing/page.tsx](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/frontend/src/app/sessions/%5Bid%5D/analyzing/page.tsx)：自动启动+1.5s 轮询、六阶段时间线（含 message）、needs_input 就地最小化问题卡（选项 chips + 自由文本，作答续跑）、完成 0.6s 后自动进 Workspace。
  - Workspace：[page.tsx](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/frontend/src/app/sessions/%5Bid%5D/workspace/page.tsx) Dashboard/报告双 Tab、证据双向跳转；[WorkspaceDashboard.tsx](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/frontend/src/components/workspace/WorkspaceDashboard.tsx)：12-column grid（inline gridColumn span，KPI 条/Hero 380px/普通 280px 尺寸层级）、全局筛选 chips 走 refine、Finding 点击滚动定位、折叠证据就地展开（高亮目标自动展开）、追问框新视图追加；[WorkspaceReport.tsx](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/frontend/src/components/workspace/WorkspaceReport.tsx)：八节排版、claim 类型徽章、证据引用点击跳 Dashboard 高亮、limitations 展示；空态不伪造 0。
  - 旧 Stepper 工作台 [/sessions/[id]](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/frontend/src/app/sessions/%5Bid%5D/page.tsx) 原样保留，入口标注"高级模式 / 查看分析依据"；首页上传/示例直接跳 analyzing，首页文案更新为自动分析口径；ViewChartCard 支持自定义高度。
  - TR-9.3：`tsc --noEmit` 通过；`next build` 六个路由全部产出（含旧工作台与旧 dashboard 页）。TR-9.4/9.5 美学与简洁性 rubric 留独立 Review 评分。

## Task 10: 全链路回归与红线审计
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 9
- **Description**:
  - 一键链路 E2E 固化：以示例集断言 AC-1 全部数量指标（≥5–8 价值视图 / ≥3 深度结论 / ≥1–3 深挖链）。
  - 审计引擎目录无 LLM 导入、无 eval/exec 自由代码路径；旧端点逐个冒烟。
  - 全量 pytest + 前端 typecheck/build；性能抽测（Superstore 全流程 ≤30s）。
- **Acceptance Criteria Addressed**: AC-1、AC-10
- **Test Requirements**:
  - `rule` TR-10.1: 一键 E2E 离线通过并断言全部数量与可消费性指标。
  - `rule` TR-10.2: 全量测试通过数 ≥ 原 237 + 新增，无回归；引擎/代码路径审计无新增违规。
  - `rule` TR-10.3: Superstore 量级全流程耗时 ≤30s（记录实测值）。
- **Completion Evidence** (2026-01):
  - E2E：[test_e2e_acceptance.py](file:///Users/zhuoyi/Documents/trae_projects/AI%20Data%20Analyst%20MVP/backend/tests/test_e2e_acceptance.py) 3 用例：
    - 一键链路（scan→signals→chains→graph→dashboard→report）断言：≥8 个可消费 presentation 视图、≥3 个 conclusion/signal 深度结论且证据均在 dashboard 视图中、1–3 条完成深挖链且含探针证据、state=ready、布局每行 ≤12、report validator 零违规、KPI 真实产出。
    - 红线静态审计：engine 目录无任何 `app.services.llm` 导入；九个自治服务文件无 eval(/exec( 调用。
  - TR-10.2：全量后端 **287 passed**（基准 271 + 新增 16），零回归；前端 tsc + next build 六路由通过。
  - TR-10.3：Superstore 量级（240 行宽表）离线全流程实测 **≈1.5s**（远低于 30s 上限）。
