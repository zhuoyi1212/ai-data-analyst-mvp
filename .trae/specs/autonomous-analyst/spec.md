# Autonomous AI Data Analyst - 产品需求文档

## Overview
- **Summary**：把产品从"上传 → 字段确认/质量/问题/方案逐步引导 → 最后一张图"的浅层工具，重构为"上传任意结构化数据 → AI 自动理解 → 自动多维探索与信号排序 → 对高价值信号自动多层深挖 → Tableau Public 级交互 Dashboard + 深度分析报告 → 追问/下钻"的真正 AI Data Analyst。
- **Purpose**：作品集核心项目，展示 AI 自动探索编排、证据图谱、确定性计算与证据链的工程深度，同时达到可被招聘方直接感知的产品完成度。
- **Target Users**：无技术背景业务人员（上传即得分析）；作品集评审者（检验分析深度与工程严谨性）。

## Goals
- G1：一键自动分析。上传（或示例集）后系统自动完成语义理解、质量处理、广度扫描、信号排序、多信号深挖、Dashboard 与报告合成，用户无需逐步确认。
- G2：分析有深度。发现高价值信号后自动继续下钻（子维度/交叉维度/驱动指标/集中度验证），形成完整分析链，而不是停在第一层图表。
- G3：产物有层级。Dashboard 按 KPI Strip / Hero / Primary / Supporting / Diagnostic / Findings 组织（12-column）；报告按结论→证据→拆解→原因线索→影响→风险机会→建议组织。
- G4：延续红线。所有数值只来自确定性引擎；LLM 只做理解、方向选择、逻辑组织与表达；每条结论引用真实 evidence_view_ids 并通过接地校验。

## Non-Goals
- 不推翻、不重写确定性引擎、既有算子契约、run 版本化与校验门禁。
- 不做自由代码（SQL/Python）生成执行、数据库直连、实时流式数据。
- 不做正式 change-point、预测、多周期季节性（沿用原路线图 P2 延期项）。
- 不做账号体系、多人协作、云端部署、PDF/PPT 导出。
- 不删除旧工作台：旧的逐步链路保留在"高级模式"，技术细节（plan/formula/snapshot_hash/step_id）收纳其中。

## Background & Context
- 仓库：github.com/zhuoyi1212/ai-data-analyst-mvp；后端 FastAPI + pandas + Pydantic v2，前端 Next.js 14 + Tailwind + recharts；237 tests 通过。
- 现状审计结论：
  - 已落地：13 算子引擎、LLM 契约层（replay/live/record）、语义/质量模块（默认决策均为保守 keep）、Bundle 候选生成与执行校验、Dashboard 合成（KPI/视图字典/筛选真重算/8 类 Finding）、T09 预算受控 Diagnostic Search。
  - 缺口：无自动编排端点；T09 不在主链路且产出无人消费；无 Signal Detection/Ranking；分析全部停在单算子第一层；无报告产物；Dashboard 固定五段等宽、无 12 栅格与联动。
- 关键既有事实：`profiler.is_complete` 在字段全部高置信时自动返回 complete（无需用户确认）；`quality_checker` 每个 issue 都有保守默认动作，`apply_decisions` 可直接用默认决策生成快照——这两点使"自动通过前置环节"不需要改造底层模块。

## 目标链路
```
上传/示例集
  → POST /sessions/{id}/auto-analyze（单一编排端点，可轮询阶段进度）
      1. 自动语义：generate_dictionary；complete 即通过
      2. 自动质量：run_quality_checks + 默认决策 apply_decisions → snapshot
         —— 仅关键歧义/严重错误时返回 needs_input，用户作答后续跑
      3. Broad Scan：八类广度视角批量执行
      4. Signal Detection + Ranking：确定性提取信号并排序
      5. Multi-root Diagnostic：top 信号各起一个 T09 风格深挖（共享预算）→ AnalysisChain
      6. Synthesis：DashboardComposition（12-col）+ AnalysisReportArtifact
  → Workspace：Dashboard + 报告双产物
  → 追问/下钻：继承当前 scope 与口径，产物作为新视图追加，不覆盖
```

## Functional Requirements

- **FR-1 自动分析编排**：新增 `POST /sessions/{id}/auto-analyze` 一个端点完成全部阶段；`GET /sessions/{id}/auto-analyze` 返回当前阶段、阶段日志与产物状态；编排状态持久化，中断后可续跑且不重复已完成计算；幂等。
- **FR-2 智能 Gate（替代强制确认）**：自动应用语义/质量的保守默认决策。仅在以下情况 needs_input：① 存在影响核心分析的关键语义歧义（字段 unknown 且无法安全忽略）；② 无任何可用数值/比率指标；③ 快照零有效行；④ 严重质量错误导致继续分析必然失真。普通缺失值、格式转换、重复行、离群值不拦截。用户作答只针对被提出的具体问题，作答后续跑而非重跑。
- **FR-3 Broad Scan**：第一轮自动覆盖八类：核心 KPI、时间趋势、主要维度对比、Top/Bottom、结构占比、异常、指标关系、盈利/效率类指标（数据支持时）。视角数量随字段条件自适应，不硬编码固定数字。
- **FR-4 Signal Detection & Ranking**：从 scan 结果确定性提取信号（增长/下滑、结构背离、负贡献成员、异常、显著相关、率结构变化等）；每个信号携带类型、位置（维度/成员/指标）、方向、量级（引用真实 scan view 的数据，不自造数字）与多维评分；排序确定性（同输入同顺序），取 top K 进入深挖。
- **FR-5 Multi-root Diagnostic**：对 top 信号分别发起深挖（复用 T09 控制器内核：候选维度→probe→启发式评分→展开/停止），每个 root 产出一条 AnalysisChain；深挖 probe 包括子维度拆分、交叉维度、驱动指标（如折扣/单价）、集中度与少数订单验证；全部深挖共享总预算硬边界；chain 每个节点引用真实存在的 view/probe，结果可回算。
- **FR-6 Evidence Graph**：构建证据索引：scan view → signal → chain → chain 节点（view/probe）→ 报告 claim。可查询任一 view/probe 的上下游（被哪个信号/链/结论引用），支持前端从 Finding/结论跳转到对应图表并高亮。
- **FR-7 AnalysisReportArtifact**：报告包含八节：Executive Summary、核心表现、关键驱动因素、风险与异常、深度诊断、机会点、结论与建议、Evidence/Methodology。每个 claim 为结构化对象（text、claim_type: fact/signal/hypothesis/conclusion、evidence_view_ids、limitations）。LLM 基于证据目录撰写，数字接地校验逐数字匹配；不合法（未接地数字、引用不存在证据、因果违规）拦截重生成，离线场景有确定性模板兜底。
- **FR-8 Dashboard Composition**：合成器决定每个视图的角色（kpi/hero/primary/supporting/diagnostic）、12-col 跨度、行高与排序；KPI Strip 3–4 个同范围 KPI，Hero 1 个，Primary 1–2 个，Supporting 3–5 个，Diagnostic 承载深挖链视图；低价值/冗余视图默认隐藏但可展开；布局确定且可解释（每个排版决策有理由）。
- **FR-9 Dashboard 交互**：全局筛选（沿用 refine 真重算）；图表点击联动（点击维度成员筛选/高亮相关图表）；点击 Finding/报告结论高亮对应图表并滚动定位；下钻路径可视化（chain 节点可点击查看对应 view）；追问框：`POST /sessions/{id}/ask` 继承当前筛选与口径执行确定性计算，产物作为新视图/新报告段落**追加**，不覆盖原产物。
- **FR-10 旧链路保留**：现有全部端点与工作台保留可用；新增能力不改变引擎模块对 LLM 的零依赖；旧工作台入口改为"高级模式/查看分析依据"。

## Non-Functional Requirements
- **NFR-1 性能**：Superstore 量级（数千行）一键全流程在 M 系列 Mac ≤ 30 秒完成；每阶段有进度反馈而非黑盒等待。
- **NFR-2 可测试性**：全部新验收在无 key、无网络条件下通过固件回放/规则兜底重复运行；新增模块单测 + 一条一键链路 E2E。
- **NFR-3 兼容与视觉**：Chrome/Safari/Edge 桌面端；视觉保持极简、专业、低饱和；Dashboard 与报告在 1440px 宽度下信息层级清晰。
- **NFR-4 工程质量**：现有 237 测试不回归；前端 tsc --noEmit 与生产构建通过；新增契约全部 extra=forbid。

## Constraints
- **Technical**：沿用 Next.js 14 / FastAPI / pandas / Pydantic v2 / Tailwind / recharts；新增服务位于 backend/app/services，新增契约位于 backend/app/schemas。
- **Business**：单人作品集；不堆功能，重构聚焦自动链路、深度、报告与 Dashboard 四件事。
- **Dependencies**：DeepSeek key 可选（离线固件可完整验收）。

## Assumptions
- 上传数据为首行表头的标准二维表；数值只来自引擎；LLM 输出中出现的任何数字都必须能在引用证据中按展示精度匹配。
- 深挖的启发式评分是排序手段，不表述为因果解释度或统计增益；观测性结论遵守因果护栏。

## Acceptance Criteria

### AC-1：一键自动分析端到端
- **Type**：rule
- **Given**：用户上传合法数据或选择示例集
- **When**：调用 POST /auto-analyze 并轮询至完成
- **Then**：无需任何逐步确认，系统产出同一 run 版本内的 DashboardArtifact 与 AnalysisReportArtifact；Superstore 类数据产出 ≥5–8 个有价值可消费视图、≥3 个有证据支撑的深度结论、≥1–3 条深度≥2 的深挖链（数据支持时）
- **Pass Condition**：新增离线 E2E 测试断言两产物存在、视图/结论/链数量达标且全部可消费；`pytest` 全套通过且现有 237 测试无回归
- **Evidence**：E2E 测试输出、落盘产物目录

### AC-2：关键 Gate 拦截与续跑
- **Type**：rule
- **Given**：各类数据（正常数据、无指标数据、零行快照、含普通质量问题数据、含关键歧义数据）
- **When**：执行 auto-analyze
- **Then**：正常数据与只含普通问题（缺失/格式/重复/离群）的数据自动放行；仅四种严重情形返回 needs_input 并附具体问题；提交作答后从断点续跑且不重复已完成阶段
- **Pass Condition**：参数化测试覆盖五类数据，断言放行/拦截决策与续跑行为；拦截必须给出可操作中文问题
- **Evidence**：编排器单测、API 测试

### AC-3：Broad Scan 覆盖度
- **Type**：rule
- **Given**：字段条件完整的数据
- **When**：执行广度扫描
- **Then**：八类视角中数据支持的类别全部产生候选并执行；无日期列时不产生时间视角；不支持盈利派生时不产生该视角
- **Pass Condition**：测试断言各类 scan 视角按字段条件存在/缺席；所有成功视角通过既有校验门禁
- **Evidence**：scan 规划器单测

### AC-4：信号提取与排序确定性
- **Type**：rule
- **Given**：Broad Scan 结果
- **When**：提取信号并排序
- **Then**：每个信号的量级字段可溯源到具体 scan view 的真实数据；重复执行排序结果一致；top K 被选出进入深挖
- **Pass Condition**：测试断言信号字段与 scan 数据一致、排序幂等；人为构造的信号（负成员/背离/异常）均被检出
- **Evidence**：信号提取器单测

### AC-5：多信号深挖链
- **Type**：rule
- **Given**：top 信号列表
- **When**：执行 multi-root diagnostic
- **Then**：每个 root 产出 AnalysisChain；链上每个节点引用真实可回算 view/probe；总 probe/执行/深度不超预算硬边界；数据支持时至少 1–3 条链深度≥2；预算失败也计数且不重置
- **Pass Condition**：测试断言链节点引用存在、预算边界不可突破、深挖实际包含第二层及以后视角（不是第一层换皮）
- **Evidence**：深挖编排器单测（含 T09 测试不回归）

### AC-6：证据图谱可查询
- **Type**：rule
- **Given**：一次完成的自动分析
- **When**：按 view/probe/signal/claim 查询关系
- **Then**：可取得任一 view/probe 的上下游与所属 signal/chain/claim；路径完整无悬空引用
- **Pass Condition**：测试断言每个 evidence_view_ids 引用都能在图中解析；反向查询（view→引用它的 claim）可用
- **Evidence**：证据图单测

### AC-7：深度报告与接地校验
- **Type**：rule
- **Given**：证据目录
- **When**：生成报告
- **Then**：八节齐全；每个 claim 引用真实存在的 evidence_view_ids；文本数字全部通过接地校验；无因果违规
- **Pass Condition**：① 正常报告通过校验；② 人为加入未接地数字或不存在证据的报告必被拦截；③ 离线场景模板兜底仍产出完整八节且零编造
- **Evidence**：报告生成器与校验器单测

### AC-8：12-column Dashboard 布局
- **Type**：rule
- **Given**：全部视图与价值评估
- **When**：合成布局
- **Then**：每个可见视图具有角色与列跨度；KPI Strip/Hero/Primary/Supporting/Diagnostic 层级齐全；同 row 跨度总和 ≤12；低价值视图默认隐藏
- **Pass Condition**：测试断言跨度约束、角色齐全、每个排版项有理由；布局对同输入确定性
- **Evidence**：布局合成器单测

### AC-9：Dashboard 交互与追问追加
- **Type**：rule
- **Given**：已发布 Dashboard
- **When**：使用筛选、图表联动、Finding 高亮、下钻跳转、追问
- **Then**：筛选触发真重算；点击图表维度成员联动相关图表；Finding/结论点击高亮并定位对应图；下钻路径节点可打开对应 view；追问产物以新视图/新段落追加，原产物不变
- **Pass Condition**：API 测试断言 ask 产物追加且 run 内原 view 数量/内容不变；前端交互有组件测试/走查证据
- **Evidence**：ask API 测试、前端走查

### AC-10：旧能力与引擎红线保留
- **Type**：rule
- **Given**：重构后代码库
- **When**：审计依赖与回归测试
- **Then**：旧全部端点可用、旧工作台可进入；引擎目录不导入 LLM 客户端；全部旧测试通过
- **Pass Condition**：静态依赖检查 + 旧测试套件全绿
- **Evidence**：测试输出、依赖审计

### AC-U1：分析深度与专业度
- **Type**：rubric
- **Dimension**：产物是否呈现"结论→证据→深入拆解→原因线索→影响→风险/机会→建议"的完整分析链，且结论不停留在第一层事实
- **Scale**：1-5
- **Anchors**：1 = 全是"某品类最高/某地区最低"浅层事实；3 = 有深挖但链路过短或证据单一；5 = ≥3 条结论具备多层下钻、多证据交叉、量化影响与明确后续建议，达到资深分析师报告水平
- **Pass Threshold**：≥4
- **Evidence**：Superstore 完整走查产物，独立 Review 打分

### AC-U2：Dashboard 美学与信息层级
- **Type**：rubric
- **Dimension**：Dashboard 是否达到 Tableau Public 优秀作品水准——层级、尺寸差异、留白、配色克制、交互反馈
- **Scale**：1-5
- **Anchors**：1 = 等宽白卡堆图；3 = 有层级但视觉/交互一般；5 = KPI/Hero/Primary/Supporting 层级一目了然，12 栅格布局有节奏，联动/高亮/下钻自然，低饱和专业感
- **Pass Threshold**：≥4
- **Evidence**：1440px 完整走查截图，独立 Review 打分

### AC-U3：主流程简洁性
- **Type**：rubric
- **Dimension**：用户主流程是否真正做到"上传→看 AI 分析→直接用产物"，且被拦截时问题最小化
- **Scale**：1-5
- **Anchors**：1 = 仍须逐步操作；3 = 自动跑通但进度不透明；5 = 一键完成、阶段进度可见可展开依据、拦截问题最少且可快速作答，技术细节全部收纳进高级模式
- **Pass Threshold**：≥4
- **Evidence**：新用户视角完整走查，独立 Review 打分

## Open Questions
- [ ] 深挖总预算默认值（建议：≤3 条 chain、总 probes ≤12、总终端执行 ≤36）——实现时可按 Superstore 验收用时调整，不改变"预算硬边界"原则。
- [ ] 自动 Gate 是否需要把"高离群率（>20%）"列入拦截——默认不拦截（仅在报告中标注局限），如 Review 认为误导风险高再调整。
