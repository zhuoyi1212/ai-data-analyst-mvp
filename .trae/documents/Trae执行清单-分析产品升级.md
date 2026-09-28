# Trae 执行清单：AI Data Analyst MVP 分析产品升级

基线：`zhuoyi1212/ai-data-analyst-mvp`，提交 `215673516001f8eaff750bd0a55023b7059383f0`（2026-09-26 审查）。这是待用户审核的任务清单，不表示已实施或获准直接修改代码。

## 开发边界

- 保留现有 Pandas 确定性引擎、有限算子、AnalysisPlan 和 View 独立失败机制。
- LLM 只产生类型化分析请求、假设和有证据引用的表达，不生成执行代码，不计算或补写业务数字。
- 不做多 Agent、企业权限、数据仓库、自定义 BI 编辑器。
- 展示用语精简；详细证据折叠。按以下依赖顺序实施，不先重画大量前端页面。
- S 约 0.5–1 开发日；M 约 2–3 日；L 约 4–6 日。均为熟悉当前代码的单人估计，包含专项验证，不是交付承诺。

## 已复现的验收反例

1. 旧 Sales 合计 300、Profit 合计 30；新 Bundle 调整主指标顺序但未执行后合成 Dashboard，Profit 被显示为 300，Sales 被显示为 30。
2. 筛选得到零行后 sum 返回 0，coverage=fail、passed=false，但 View status=success，Dashboard 收录 KPI=0。
3. time_series 输出 Date/value，ChartSpec 却要求 Date/Sales。
4. 全部数值有效、Pearson r=0 的 100 行数据仍选中 relationship View。
5. 每日销售 100，1 月完整 31 天、2 月仅 10 天，按月比较得到 -67.7%。
6. Profit 从 -100 到 +100，period_compare 返回 -200%，Dashboard 返回 +200%。
7. 2023 年 1 月=100、2 月=200，2024 年 1 月=150；yoy 算子比较年度累计得到 -50%，不是 1 月同比 +50%。
8. ending_stock/satisfaction/conversion_rate/profit_margin 的默认聚合均为 sum。
9. Profit 为 0…99 和一个合法 -1000 时，质量建议排除负利润；采纳后合计由 3950 变为 4950。
10. 锁文件全新环境中 Spearman 路径缺少 scipy，执行报 ModuleNotFoundError。

现有 184 项测试通过，上述反例未被现有验收覆盖。Superstore 原始文件不在仓库内，文档中的 Superstore 数字未独立复算。

## P0：先修可信度

### T01 分析运行版本与失效（M）

涉及：storage.py、routers/bundle.py、routers/dashboard.py、bundle_executor.py、dashboard_synthesizer.py。

- 每次运行固定 run_id、bundle_id、snapshot_hash、dictionary/metric_spec 版本、scope_hash、plan_hash。
- View、execution、probe、dashboard 按运行版本存储。当前运行指针仅指向已发布的完整版本；旧运行可读，但明确历史身份。
- 新 Bundle、新数据快照、语义/指标口径修改后，旧下游不得作为当前产物消费。
- 合成与读取校验版本匹配；错配返回 409/stale，禁止仅判断文件存在。
- 同一运行重复合成应幂等，不重置 probe 预算；旧请求晚返回不得覆盖新结果。

验收：反例 1 阻断；新 Bundle 未执行时不能生成当前 Dashboard；快照变更、指标重排、视图数减少、重复请求及迟到响应均无混用。

### T02 质量处理保护业务信号（M）

涉及：quality_checker.py、quality_actions.py、demo 脚本。

- 区分物理错误、业务合法但极端的值、尚未确定的异常。
- Profit 允许负数；负库存可能是业务问题，不能仅依据分布判为录入错误。
- IQR 离群默认标记/保留；只有明确的字段业务约束支持排除建议。
- 数值缺失不默认填均值；分类缺失默认保留 Unknown 桶。明确缺失对当前指标的覆盖影响。
- 合法重复交易不能仅因整行重复就认定错误；结合业务键与数据粒度。
- 展示清洗前后关键总量与删除/插补影响。插补值不得混入未标记的实际经营总量。
- 演示脚本不得自动采纳会改变经营事实的处理建议并将其伪装为真实用户确认。

验收：反例 9 保留亏损；正常高额订单保留；缺失金额不伪造收入；质量报告包含口径影响。

### T03 最小 MetricSpec（M）

涉及：schemas/dictionary.py、derived_metrics.py、bundle_planner.py、engine/ops.py。

- 补充 grain、entity_key、aggregation、unit、direction、time_role、allowed_negative、null_policy。
- 支持 additive、ratio_of_sums、count_distinct、snapshot_last、明确适用的 mean/weighted_mean。
- Stock 的时间口径为每实体期末快照再汇总；Orders 依赖订单键去重；不能把行数自动称为订单数。
- 比率分子分母同范围、同粒度、同单位体系，记录分母与有效行覆盖；没有分母时不宣称已正确加权。
- 字段名只产生候选口径；关键含义有歧义时单点澄清，不展开全字段确认流程。
- 非可加总指标不得生成“各组均值/均值之和”的份额分析；负值组成禁用饼图。

验收：反例 8 修正；对销量、库存、满意度、转换率分别给出正确或明确不支持的口径；不得静默求和。

### T04 View 与 Probe 校验门禁（S）

涉及：bundle_executor.py、schemas/bundle.py、dashboard_synthesizer.py、dashboard_insight.py。

- 执行状态和结果有效性分开：success/failed 与 pass/warn/fail/no_data。
- KPI、Chart、Finding 共享同一 consumable 判断；probe 使用相同门禁。
- coverage 检查实际有效样本，保留 null 排除数；算子适用时执行分组回总、份额回总、计划/快照一致性校验。
- 预期的零分母 null 与非法 NaN/Inf 分开处理；无数据不得假装销售为 0。

验收：反例 2 不进入正常展示；其他正常 View 继续完成；分母零显示“不可计算”。

### T05 可比时间与变化口径（M）

涉及：engine/ops.py、schemas/plan.py、dashboard_synthesizer.py、dashboard_insight.py。

- PeriodSpec 显式记录当前起止、比较起止、时区、粒度、数据截至日、完整性状态。
- 分离 period granularity 与 comparison mode，支持月同比、环比及明确的同期累计。
- partial/unknown 完整性不触发正常完整周期告警；MTD 只比较等长同期。
- max(event_time) 仅是完整性线索，不视为已验证的数据截至日。
- KPI value、change、Finding 必须共享同一周期/筛选范围；全时段累计单独标注。
- 正基数比较可展示增长率；负基数、跨零、近零基数默认绝对差额与“扭亏/转亏/亏损收窄”等状态；率的变化默认百分点。

验收：反例 5/6/7 全修正；覆盖首尾残缺期、缺失中间期、闰年、零/近零分母、率百分点。

## P1：形成分析闭环

### T06 前端可消费的 Dashboard 契约（M；依赖 T01/T04/T05）

- Dashboard 返回视图字典，包含 ChartSpec、数据 schema、数据或可访问 data_ref、口径与验证状态。
- ChartSpec 的 x/y 字段必须存在于输出 schema；业务指标名称与绘图字段名分开。
- 聚合数据可内嵌，明细分页；散点采样需有 total_count/display_count/sample_method，统计计算基于全量有效样本。
- 全局筛选改变时重算聚合、比率、Findings，不在已聚合数据上简单隐藏条形；整批结果原子发布。
- 提供历史运行、部分失败、陈旧结果和空态。

验收：浏览器只靠 API 即可绘图，不接触服务器 parquet；字段错配拒绝；筛选后所有可见内容一致。

### T07 Seed 与结果价值筛选（M；依赖 T03/T05）

- 初始只执行有意义的核心 KPI、两个核心指标的对齐趋势、一个基准拆分；不按图表类型凑数量。
- 区分内部计算任务与展示 View；一个业务问题可组合多个计算结果。
- 执行后按数据有效性、业务影响、证据质量、新证据、冗余与展示成本筛选。
- 去重键涵盖 metric/scope/period/question；结果层合并指向同一问题的 Findings。
- r 很弱、没有异常、只有重复信息的分析默认隐藏；用户明确问到时仍可返回阴性结果。

验收：r=0 不占默认视图；无信号数据不凑 Findings；双指标趋势不被槽位挤掉。

### T08 跨指标信号与可复核分解（M；依赖 T03/T05/T07）

- 第一版只做三类：金额与盈利/效率背离、分组变化贡献、率的结构变化。
- 适配 Sales/Profit/Margin；Orders/AOV 仅有正确业务键时启用；Revenue/Conversion 仅有事件分母时启用。
- 分解返回分组当前值、基期值、差额、占比/分母、未解释部分；所有加法恒等式可回算。
- 贡献是会计拆解，不称因果；正负贡献抵消时不用不稳定净变化百分比误导。
- 利润总计为负或接近零时，不把“利润份额”当普通组成比。

验收：规模增长但盈利下降、各组改善但整体率下降、稳定分布三个黄金案例。

### T09 预算受控的 Diagnostic Search（L；依赖 T01/T04/T08）

- 单控制器，最多 4 轮、自适应诊断 probe 总数最多 8，维度过滤路径深度最多 3。
- 每个维度试探算 1 probe；每 probe 最多 3 个白名单终端计划，总计最多 24 个额外终端执行。失败也耗预算，重试和恢复不重置。
- 首轮测试至多 3 个候选维度，保留最多 2 个分支；后续只展开当前最有价值分支，不铺满树。
- 候选覆盖 Category/Region/Segment 等低中基数维度；高基数 Customer/Product 有基数惩罚与最小独立实体数；Discount 通过固定分箱；时间需要可比周期。
- 评分保存 concentration、impact、support、stability、novelty、cardinality_penalty；明确这是启发式评分，不是因果解释度或统计信息增益。
- 比较范围继承父节点与根问题的 PeriodSpec；拒绝空分组、单成员分组、重复 scope 和不适用字段。
- 停止：无信号、数据不足、影响不足、连续两次无新证据、同分支连续失败、预算/时限/深度耗尽。
- 每轮产生可核实的简短动作摘要、结构化 observation、下一步与 stop_reason；不输出模型内部思维过程。
- 规则降级路径必须可演示；LLM 最多 4 次选择与 1 次最终综合，输出必须过业务契约。

验收：预算严格封顶；不存在“扫描几十维算一次 probe”；弱信号提前停；失败不伪造结论；中断恢复不重复或超额。

### T10 Finding 证据结构与关系分析（M；依赖 T04/T08）

- Finding 包含现象、位置、量化影响、重要性依据、atomic claims、证据、局限、下一步验证。
- claims 分为 fact/signal/hypothesis/conclusion；hypothesis 有 proposed/supported/refuted/inconclusive 状态。
- 证据引用运行、结果、步骤、数值键、筛选、周期和 probe；严禁只有 view_id 而找不到下钻数字。
- importance 与 evidence_quality 分离；不使用未经校准的“95%置信”标签。
- 负相关不自动 risk；根据指标改善方向、业务语义和实质影响判断。
- Pearson/Spearman 检查有效成对样本、常量、异常点敏感性、重复实体；补齐 Spearman 所需运行依赖。
- categorical×numeric 用分组效应/率差，不能将类别编码后算 Pearson。
- 预先限制业务相关候选；大量试探时记录多重比较和探索性身份，不将最强相关直接升级为验证结论。

验收：反例 10 可用；负相关的响应时长×满意度不自动归为盈利风险；弱相关阴性验证可保留证据但不占默认图；每个数字有同运行来源。

### T11 Dashboard 主工作区与追问（L；依赖 T06/T09/T10）

- 首屏：3–4 个同范围 KPI、最多 3 条重要 Finding、3–5 个必要图；不强制五个空分区。
- 点击 Finding：显示问题、诊断路径、贡献数据、已验证事实、未验证假设与证据。
- Assistant 继承当前筛选、周期、指标口径、选中 Finding 和运行版本；区分解释已有证据与执行新分析。
- 追问产物追加为新视图/发现，保持来源与前后关系；不覆盖原始 Dashboard。
- 主界面移除强制选题和逐图确认；只在会改变结论的关键口径歧义处提问。

验收：上传→发现→诊断→证据→追问追加形成一条完整流程；刷新后可恢复。

### T12 产品价值评测与作品集演示（M；贯穿 T01–T11）

- 准备带真值的诊断案例：已知问题、无问题、结构反转、残缺周期、异常点假相关、数据不足。
- 比较静态 Bundle 与受控 Loop：定位正确率、误报率、是否拒绝无效结论、证据完整率、平均 probe 数、耗时。
- 至少零售和营销两种数据演示语义迁移；没有分母的数据必须明确拒绝转换率计算。
- 主演示突出：跨指标发现问题→选择解释力较好的维度→验证/否定假设→动态补图。
- 作品集中的数值提升仅在评测后填写，不把测试数量或图表数量当分析价值。

## P2：仅在主链路稳定后考虑

- S：rolling mean、相邻窗口加减速、候选转折提示；不能冒称正式变点检测。
- M：数据足够时加入季节性基线与稳健残差异常。
- L：正式 change-point、预测、多周期季节性；目前延期。
- 暂不做任意业务指标知识图谱、通用统计模型库、复杂拖拽 Dashboard、多 Agent。

## 建议实施批次

1. T01–T05，先锁住真实数据、指标与比较口径。
2. T06–T08、T10，做出一个可渲染且有真实分析价值的竖向切片。
3. T09、T11，以最多 8 次 probe 完成自动诊断与追问。
4. T12 持续验收，再决定是否投入 P2。
