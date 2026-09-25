# Phase 2 实施计划：DerivedMetric 确定性比率 → DashboardArtifact（后端，不动前端）

## Repository Research

Phase 1 已落地并经用户验收（163 测试通过，Superstore 8/8）：
`schemas/bundle.py`、`services/bundle_planner.py`（Candidate Generation → RuleBundleSelector，Protocol 可换 LLM）、
`services/bundle_executor.py`（execute_plan 内核 + partial success + view 级三件套落盘）、`routers/bundle.py`。

重构方案（`.trae/specs/ai-data-analyst-mvp/AI_Data_Analyst_MVP_重构方案_Trae.md`）对 Phase 2 的既定要求：
- §6.2/§37：`profitability` ViewType（Phase 1 契约已占位）；
- §10：`DashboardArtifact{kpis, sections, findings, risks, global_filters}`、`KPI`、`DashboardSection`；
- §19：`Finding{finding_id,title,summary,type,evidence_view_ids,importance,drilldown}`；
- §20：自动下钻（利润负贡献成员 → 下一层维度定位）；
- §25：`ChartSpec{type,title,xField?,yFields?,dimension?,metric?,interactive?}`；
- §34/§35：dashboard_synthesizer = 规则为主（KPI/图表类型/分区），LLM 未来只做选择/标题/排序/洞察；
- §53：禁止「A 比 B 高」式描述，Finding 必须含 发生了什么/在哪里/影响多大/关注建议。

用户对 Phase 2 的特别约束：
1. **第一优先级：确定性 DerivedMetric/ratio（Profit Margin = Profit / Sales），禁止 LLM 直接计算**；
2. 延续 Phase 1 工作方式：完成后先用 Superstore 实测 DashboardArtifact JSON + Findings 摘要验收，再进 Phase 3（前端）。

关键现状约束：
- 算子白名单是信任边界：新增算子需同步 6 处——`schemas/plan.py`（Params+Step+Union）、`engine/catalog.py`（OP_META/step_columns/字段校验）、
  `engine/ops.py`（实现+_DISPATCH）、`offline_fallback.build_plan`（hint→Params）、AST/白名单相关测试；
- 引擎目录禁止 import llm（有守卫测试）；
- 五阶段 AI 均有离线确定性降级路径——Phase 2 的 Findings 同样必须纯规则可跑（replay/无 key），LLM 仅作未来增强；
- 后端 8000/前端 3100 正在运行（旧进程），验证只走 pytest + 脚本，不重启服务。

## Files and Modules

新增：
- `backend/app/schemas/derived_metric.py`：`DerivedMetricSpec`（比率派生指标契约）。
- `backend/app/services/derived_metrics.py`：确定性比率注册表与探测（纯规则，无 LLM）。
- `backend/app/schemas/dashboard.py`：`ChartSpec`、`KPI`、`DashboardSection`、`FilterDefinition`、
  `DrilldownSuggestion`、`Finding`、`DashboardArtifact`（全部 extra="forbid"）。
- `backend/app/services/dashboard_insight.py`：跨视图规则 Findings 检测器（每类一个纯函数 detector）。
- `backend/app/services/dashboard_synthesizer.py`：Bundle + View Results → DashboardArtifact；
  含确定性 drilldown probe 执行（复用 execute_plan）。
- `backend/app/routers/dashboard.py`：`POST/GET /sessions/{id}/dashboard`。
- `backend/tests/test_derived_metrics.py`、`test_dashboard_synthesizer.py`、`test_dashboard_api.py`。
- `backend/scripts/run_dashboard_demo.py`：Superstore 端到端实测脚本（复用 run_bundle_demo 的管线准备）。

修改：
- `backend/app/schemas/plan.py`：新增 `DeriveRatioParams/DeriveRatioStep` 并入 Union（白名单第 11 个算子 `derive_ratio`）。
- `backend/app/services/engine/catalog.py`：登记算子（terminal, chart=metric/bar 按是否带 dimension 决定→固定 ChartType.bar，单值场景由 synthesizer 取 KPI）、step_columns、双指标字段语义校验。
- `backend/app/services/engine/ops.py`：`op_derive_ratio`（ratio-of-sums 语义，可选 dimension/date+granularity）+ _DISPATCH。
- `backend/app/services/offline_fallback.py`：build_plan 增加 derive_ratio 分支。
- `backend/app/services/bundle_planner.py`：profitability 候选生成 + 选择器槽位策略（见下）。
- `backend/app/schemas/bundle.py`：profitability 的 VIEW_CHART 由占位 metric 改为 bar（真正启用）。
- `backend/app/services/storage.py`：dashboard artifact 槽位（bundle/dashboard.json）+ probe 目录（bundle/probes/{probe_id}/ 三件套）。
- `backend/app/main.py`：注册 dashboard 路由。
- `.trae/specs/ai-data-analyst-mvp/tasks.md`：追加 Task 26（Phase 2）。

## 关键设计

### 1. DerivedMetric / ratio（确定性，禁止 LLM 计算）
- 契约 `DerivedMetricSpec`：`key/label/numerator/denominator/agg("ratio_of_sums")/unit(%)/source("rule.derived")`。
  **语义铁律：比率 = Σnumerator / Σdenominator（先聚合后相除），绝不允许对行级比率求平均**（防 Simpson/口径错误），该约束写进算子 docstring 与专项测试。
- 注册表第一版：`profit_margin`（利润/profit/margin 类 ÷ 销售|营收|revenue|sales|amount|收入）。
  结构设计为可扩展模板（未来 gross_margin、ROI、客单价等只加规则，不改架构）；探测不到就不产出，不强行造指标。
- 引擎新算子 `derive_ratio`：参数 numerator/denominator，可选 dimension（按成员出比率）、date_column+granularity（趋势比率，预留）；
  分母为 0/NaN 时该成员输出 null（不抛断全 Bundle），summary 回传 total_num/total_den/null_member_count。
- 比率参与三处：① KPI 卡（整体利润率）；② profitability View（锚点维度各成员利润率）；③ Findings 证据数值。
  所有比率数值只经该算子或 execute_plan probe 产出，synthesizer/insight 只做比较与文案，不做四则运算以外的构造，且不引入 LLM。

### 2. Planner 槽位策略（4–8 上限不变）
- 探测到比率时新增 profitability 候选（锚点 = breakdown 维度，op=derive_ratio）。
- 策略：**profitability 启用时替换 ranking 的槽位**（双 P&L 数据集叙事链：总览→趋势→Segment→Category 销售/利润/利润率→关系）；
  无比率数据集维持 Phase 1 行为（ranking 保留）。理由：ranking（如 Top5 客户名）在自动洞察中复用率最低；
  若用户在评审时更偏好保留 ranking，可改为「max_views 弹性到 8 内替换 anomaly」等其他策略——以本计划批准为准。
- 同维度去重规则放宽：同一维度允许 2 个原始指标 View + 派生比率 View（派生指标不占原始指标冗余额度，但总 View 数仍 ≤8）。

### 3. DashboardArtifact 合成（规则版）
- `KPI`：取 overview views 的 Sales、Profit；新增 Profit Margin KPI（derive_ratio 无维度单值，probe 执行一次）；
  trend 存在 ≥2 个时间点时给最新月环比 change（末两期确定性差值，change_type="mom"，附口径说明）。
- `sections`：固定五段 overview/trend/structure/diagnosis/detail，按 view.type 映射（profitability→diagnosis；
  relationship/ranking 等→diagnosis 或 detail），每段带 view_ids。
- `ChartSpec`：由 view.type + plan.params 确定性生成（line: xField=date/yFields=[metric]；bar: dimension+metric；
  pie: dimension+metric；scatter: xField/yFields；metric KPI 卡不出 chart）。interactive 对维度图置 true（Phase 3 联动用）。
- `global_filters`：从 primary_dimensions 推荐低中基数维度（≤30）作为 FilterDefinition（column/label/members 由快照取值）。

### 4. 跨视图 Findings（dashboard_insight.py，纯规则 + 阈值 + 中文模板）
- `divergence`（量利背离）：对照对（同维度 Sales vs Profit）中 profit 为负或利润份额显著低于销售份额（差 ≥10pp）→ high/medium，
  evidence 两个 view_id。
- `risk.negative_member`：维度成员 Profit<0 → risk；自动跑 **drilldown probe**（filter 该成员 → group_by Profit 于下一推荐维度，
  如 Category→Sub-Category），定位 Top2 负贡献子成员，生成 DrilldownSuggestion{filters, child_dimension, top_negative_children}；
  probe 失败/无下一层维度时 finding 仍保留、drilldown=None（partial success 原则）。
- `relationship`：相关 View summary 中 |r|≥0.3 出 finding（负相关→risk 文案），固定附加「数据关联不等于因果」免责语。
- `trend`：末两期变化 ≥10% → growth/decline（值与 view 结果一致）。
- 所有 Finding 必须四要素：现象/位置/量化影响/关注建议；模板内数值全部来自 view result 或 probe；
  findings/risks 双列表（risks = type=risk 子集）。LLM 增强接口同 BundleSelector 模式预留 Protocol（本阶段不实现）。

### 5. API / 存储 / 兼容
- `POST /sessions/{id}/dashboard`：要求 bundle + execution 存在（409/404 口径同 bundle 路由）；合成落盘 dashboard.json，返回完整 artifact。
- `GET`：读回 artifact。
- probe 产物隔离在 bundle/probes/{probe_id}/{plan.json,result.parquet,ledger.json}，不触碰 view 与旧槽位。
- Phase 1 接口、旧十阶段接口行为不变。

## Implementation Steps（依赖序）

1. 白名单第 11 算子：plan schema → catalog → ops.derive_ratio → build_plan 分支 → 算子单测（含 ratio-of-sums 正确性、分母 0、维度口径、AST 守卫同步）。
2. derived_metrics：注册表 + 探测 + schemas/derived_metric.py；单测（Superstore 字段形态命中、无利润字段不命中、混合语言命名）。
3. Planner：profitability 候选 + 选择器策略 + 同维度放宽；更新/新增 planner 测试；全量跑 5 示例 + 合成数据。
4. schemas/dashboard.py 全套契约（先 schema 后服务，extra=forbid）。
5. storage dashboard/probe 槽位。
6. dashboard_synthesizer：KPI（含 margin probe、mom）→ sections → ChartSpec → filters；单测用既有 _ready_session 真实管线。
7. dashboard_insight：四类 detector + drilldown probe；单测覆盖：Furniture 类负成员必出 risk+drilldown、弱相关不出、免责语存在、证据 view_id 真实存在。
8. routers/dashboard.py + main 注册；api 测试（门禁 409/404、合成与 GET 恢复）。
9. run_dashboard_demo.py：Superstore 实测，打印 DashboardArtifact JSON、KPI、各 Section、Findings（含下钻定位）。
10. 全量 pytest 零回归；tasks.md 追加 Task 26；向用户展示 Superstore 实测结果等待验收 Gate（不进 Phase 3）。

## Dependencies and Considerations
- pandas 3.0.6（无 Int64 大写 dtype）；数值清洗复用 executor.clean_rows；probe 写 parquet 前同样需兼容混合类型（复用 save_view_result 同款规范化路径）。
- 比率 KPI 与 profitability view 都要经过 execute_plan，保证证据链（step records + formula）一致。
- mom 环比要求时间序列末两期可比（月度且数据覆盖末两期）；不满足则 change=None，不造数。
- ChartSpec 是 Phase 3 前端契约，本阶段字段一次定稳但前端不消费；避免引入堆叠双指标 spec（双指标线图留待 Phase 3 交互需求）。
- 不重启服务、不改前端、不 git commit/push。

## Validation
- `cd backend && uv run pytest -q`：现有 163 全过 + 新增测试（预计 25–35 例）零回归。
- 专项断言：margin = ΣProfit/ΣSales（Superstore ≈ 12.23%）；Category 利润率 Furniture 为负；
  Finding 证据数值与 view result.parquet 完全一致；risk finding 必带 drilldown（Furniture→Tables 等真实子成员）。
- Superstore 脚本：DashboardArtifact 落盘且 8 View 全部可在 sections 中追溯；API TestClient 走通合成/读取。

## Risks
- **风险 1：比率口径误用行级平均** → 算子内强制 ratio-of-sums 并写专项测试（构造组间规模差异数据，两种口径结果不同，锁定期望值）。
- **风险 2：planner 槽位策略用户不认可（替换 ranking）** → 在计划评审中显式确认；批准后实现；实测 Gate 可再次调整。
- **风险 3：drilldown probe 增加执行时长/失败面** → probe 数量上限（每个负成员 1 次、总计 ≤3），异常收敛为 drilldown=None，不影响 artifact 生成。
- **风险 4：规则文案被误判为「A 比 B 高」式低价值描述** → Finding 模板强制四要素 + 数值/位置/建议齐全；单测断言 summary 同时包含成员名、数值、建议语。
- **风险 5：新增算子污染 LLM 信任边界** → 算子不暴露给自由提问的 LLM 规划（build_plan 仅供 bundle 确定性路径调用）；引擎守卫测试覆盖新算子登记一致性。
