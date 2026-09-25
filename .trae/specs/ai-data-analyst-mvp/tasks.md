# AI Data Analyst MVP - 实现计划（任务队列）

任务按依赖排序；`AC` 指 `spec.md` 中的验收标准。每个任务至少含一条 TR（`rule` 或 `rubric`），实现者须逐条自测并记录证据。

---

## Task 1: 工程骨架（后端 FastAPI + 前端 Next.js）

- **Status**: `completed`
- **Completion Evidence**:
  - `uv sync` 成功（Python 3.11 托管环境，pandas 3.0.6 / fastapi / pydantic v2 / openai 等就位）；`uv run pytest` → 1 passed（/health）。
  - 前端手动脚手架（Next 14.2.18 + React 18 + TS + Tailwind 3 + Recharts），`npm install` 与 `npm run build` 成功（/ 静态产出）。
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 后端：`backend/` 使用 uv 初始化 Python 3.11+ 虚拟环境与 `pyproject.toml`（fastapi、uvicorn、pandas、openpyxl、pydantic>=2、openai、python-dotenv、httpx、pytest）；建立 `app/` 包结构（main、config、routers、services、schemas）与 `/health` 接口；`.env.example`（LLM_BASE_URL 默认 `https://api.deepseek.com`、LLM_API_KEY、LLM_MODEL 默认 `deepseek-chat`、LLM_FIXTURE_MODE 等）。
  - 前端：`frontend/` 建立 Next.js（App Router）+ TypeScript + Tailwind 工程，最小布局与 API 客户端占位。
  - 根目录忽略规则（`.gitignore`：node_modules、.venv、.env、storage、`__pycache__`）。
- **Acceptance Criteria Addressed**: AC-U3
- **Test Requirements**:
  - `rule` TR-1.1: 后端 `uv run uvicorn` 可启动且 `GET /health` 返回 200；前端 `npm run build` 通过且 `npm run dev` 可打开首页；证据：命令输出。
  - `rule` TR-1.2: `uv run pytest` 与前端 `npx tsc --noEmit` 均可在空工程下成功退出；证据：命令输出。

## Task 2: LLM 客户端与录制/回放固件层

- **Status**: `completed`
- **Completion Evidence**:
  - 实现 `services/llm/`：OpenAI 兼容 JSON 调用（超时/重试）、契约校验 + ≤2 次修复、`replay/live/record` 三态固件。
  - `uv run pytest` → 6 passed：回放不触网、缺 key 报 LLMConfigError、修复成功、耗尽抛 ContractError、缺固件显式失败。
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 实现 OpenAI 兼容 JSON 调用封装：超时（60s）、有限重试（≤2 次指数退避，仅对可重试错误）、统一异常类型（配置错误/网络错误/契约错误）。
  - 录制/回放机制：按「阶段名 + 请求规范化哈希」将请求/响应存为 `tests/fixtures/llm/*.json`；模式由环境变量切换（off/live/record/replay）；replay 下无网络访问。
  - 通用 `generate_json(prompt, schema, stage)`：解析 JSON → Pydantic 校验 → 失败注入修复提示重试 ≤2 次 → 耗尽抛 `ContractError`（供 AC-15 统一降级）。
- **Acceptance Criteria Addressed**: AC-6, AC-15
- **Test Requirements**:
  - `rule` TR-2.1: replay 模式下使用录制固件返回预期对象且不发起网络请求；证据：pytest（禁用网络或断言调用计数）。
  - `rule` TR-2.2: 缺少 API key 且非 replay 模式时抛明确配置错误；证据：单测。
  - `rule` TR-2.3: 连续 3 次畸形 JSON 触发 `ContractError`，无静默兜底；证据：单测（畸形固件）。

## Task 3: 会话存储与上传解析

- **Status**: `completed`
- **Completion Evidence**:
  - 实现 parser（编码/分隔符嗅探、XLSX/XLS、原始表头查重查空、20MB/20 万行/50 列边界）、SessionStore（原文件/meta/preview/snapshot 落盘）、sessions 路由。
  - `uv run pytest` → 15 passed：utf-8/gbk CSV、XLSX、超限/超行/超列/错误后缀/空头/重复头拦截、落盘与 API 400。
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 文件型会话存储 `storage/{session_id}/`（原始文件、快照、各阶段 JSON）；创建会话、读取会话、阶段产物读写 API。
  - 解析器：CSV 编码/分隔符嗅探（utf-8/utf-8-sig/gbk）、XLSX 首工作表读取；边界校验（≤20MB、≤200,000 行、≤50 列、白名单后缀、非空二维表）；输出行列数、dtype、前 10 行预览。
  - 示例数据集挂载：`sample_data/` 可一键创建会话。
- **Acceptance Criteria Addressed**: AC-1
- **Test Requirements**:
  - `rule` TR-3.1: 参数化测试覆盖 utf-8/gbk CSV、XLSX、超大小（构造）、超行、超列、非法后缀、空文件/单行文件，均符合 spec 预期并返回中文原因；证据：pytest。
  - `rule` TR-3.2: 上传后会话目录包含原始文件且会话元数据可恢复；证据：单测断言落盘结构。

## Task 4: 结构化契约与有限算子目录定义

- **Status**: `completed`
- **Completion Evidence**:
  - `schemas/common.py` 枚举体系 + `schemas/plan.py` 10 算子判别联合（extra=forbid）；`engine/catalog.py` 注册表、人读模板、图表映射与字典级校验。
  - `uv run pytest` → 24 passed，含未知算子/幽灵字段/时间算子非日期/已忽略字段/参数类型非法用例，以及「无 eval/exec/subprocess/read_sql、引擎零 LLM 导入」审计测试。
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - Pydantic 模型：语义档案与数据字典、质量问题/决策、推荐问题、分析方案（步骤参数按算子区分的判别模型）、执行台账/结果、洞察、追问；公共枚举（语义类型、置信度、算子 ID、图表类型、校验状态）。
  - 算子注册表：附录 A 的 10 个算子 ID、参数 schema、人读模板、合法性规则（如时间算子必须有日期字段）；提供「未知算子拒绝」与字段存在性校验函数。
- **Acceptance Criteria Addressed**: AC-5, AC-6
- **Test Requirements**:
  - `rule` TR-4.1: 合法方案样例通过校验；未知算子、错误参数类型、引用不存在字段、时间算子无日期字段四类非法输入均被拒；证据：单测。
  - `rule` TR-4.2: 代码库审计：除算子注册表与引擎外不存在 SQL/代码执行入口（grep `eval|exec|subprocess.*code|read_sql` 等并人工确认白名单）；证据：审计结论记录。

## Task 5: Semantic Profiler（规则推断 + LLM 补全 + 确认门数据）

- **Status**: `completed`
- **Completion Evidence**:
  - 规则层（物理类型/基数/空值率/数值统计/日期可解析率/命名模式）+ LLM 补全合并（日期/ID 规则锁定，高基数文本强制降级确认）；乘积关系与高相关列对确定性探测；确认闸门与忽略字段排除。
  - `uv run pytest` → 27 passed：日期/ID/指标识别正确、低置信字段阻断、确认后放行、乘积关系识别、API 流程。
- **Priority**: high
- **Depends On**: Task 2, Task 3, Task 4
- **Description**:
  - 确定性规则：物理类型、基数、空值率、min/max/均值、样例值、命名模式（日期/金额/率/ID）、日期可解析性、枚举识别。
  - 字段关系线索：`单价×数量≈金额` 类乘积关系与数值列高相关对（确定性计算，LLM 仅命名解释）。
  - LLM 补全：业务含义、语义类型、单位、候选含义、置信度，输出受契约束缚；置信度策略（规则证据充分可直接 high，否则 medium/low）。
  - 数据字典落盘；确认接口支持「选候选/自定义含义/忽略字段」及任意字段修正；未全部确认状态可查询。
- **Acceptance Criteria Addressed**: AC-2, AC-U2
- **Test Requirements**:
  - `rule` TR-5.1: 在 5 份数据集上，日期列、指标列、id 列的语义类型与「需确认」标记符合黄金断言（基于录制固件）；证据：pytest + 固件。
  - `rule` TR-5.2: 存在 medium/low 未确认字段时会话 `profile_complete=false`；确认/忽略后转 true 且忽略字段从后续可选字段移除（API 级断言）；证据：API 测试。
  - `rule` TR-5.3: 数据字典 JSON 含 spec 要求的全部字段属性与用户确认记录；证据：落盘样例断言。

## Task 6: Data Quality 检测器

- **Status**: `completed`
- **Completion Evidence**:
  - 四类检测（缺失含 -1 哨兵、货币/日期文本/枚举大小写/哨兵格式异常、完全重复、IQR 离群），全部确定性规则并附证据与建议动作。`uv run pytest` → 32 passed（与 Task 7 合并验证）。

## Task 7: 质量处理动作与数据快照

- **Status**: `completed`
- **Completion Evidence**:
  - 白名单动作执行（格式转换→去重→缺失填充/删行→离群保留/标记/排除）与 parquet 快照（sha256、行数）；未逐项决策拒绝生成快照。
  - `uv run pytest` → 32 passed：四类问题检出数量正确、转换后 dtype/枚举/哨兵正确、去重 102→100、排除→99、标记列、决策不完整阻断、API 200。
- **Priority**: high
- **Depends On**: Task 5
- **Description**:
  - 四类检测并产出带证据的问题清单：缺失值（数量/占比/样例位置）、类型格式异常（货币串、混合日期格式、`-1` 哨兵、枚举大小写）、完全重复行、数值离群（IQR 与 z-score 双规则可配，注明阈值与命中行）。
  - 每问题附系统建议动作；落盘质量报告（草稿态）。
- **Acceptance Criteria Addressed**: AC-3
- **Test Requirements**:
  - `rule` TR-6.1: 对 5 份预埋数据，各类问题检出数量与预埋黄金值一致（缺失数、重复行数、离群行数、格式异常列）；证据：pytest 黄金断言。

## Task 7: 质量处理动作与数据快照

- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 6
- **Description**:
  - 实现全部动作：缺失（保留/删行/固定值/均值/中位数/众数填充）、格式（接受转换：货币去符号、日期解析、枚举归一；保留文本）、重复（删除/保留）、离群（保留/标记列/排除）。
  - 逐项决策收集（未全部决策不得继续）；生成处理后快照（parquet 或规范化 CSV）并计算哈希；支持回退重生成；质量报告记录决策与快照哈希。
- **Acceptance Criteria Addressed**: AC-3
- **Test Requirements**:
  - `rule` TR-7.1: 每种动作对构造样例产生正确快照（行数/类型/标记列/填充值），证据：单测。
  - `rule` TR-7.2: 决策不完整时快照接口拒绝；完整后快照哈希与文件一致且可在报告中检索；证据：API 测试。

## Task 8: Question Recommender

- **Status**: `completed`
- **Completion Evidence**:
  - 推荐器 = LLM 生成 + 确定性后置校验（6–10 条、≥4 分类、依据必须接地字段名/特征词、算子合法且与分类匹配、无日期禁趋势）。
  - `uv run pytest` → 35 passed：合法推荐通过、无日期数据集趋势类被 ContractError 拦截、空泛依据被拦截。
- **Priority**: high
- **Depends On**: Task 5, Task 7
- **Description**:
  - 场景模板 + LLM 生成 6–10 个问题：分类（总量/趋势/对比/占比/排名/异常/相关）、依据（必须引用字段名或数据特征）、涉及字段、目标算子、置信度；无已确认日期字段时禁止时间类问题；「换一批」与自定义问题入口。
- **Acceptance Criteria Addressed**: AC-4
- **Test Requirements**:
  - `rule` TR-8.1: 契约测试：数量 6–10、分类 ≥4、每条 rationale 非空且包含字典字段名或特征词、target_op 合法；证据：pytest + 固件。
  - `rule` TR-8.2: 构造无日期字段字典，断言不返回时间类（time_series/period_compare）推荐；自定义文本问题可被规划接口接受（与 Task 9 联动，先打桩）；证据：单测。

## Task 9: Analysis Planner（方案生成 + 校验 + 修复）

- **Status**: `completed`
- **Completion Evidence**:
  - `planner.py`：LLM 仅草拟方案；AnalysisPlan 判别联合（算子白名单/禁自由代码）+ 字典语义校验 + 依赖/末端校验，业务错误同样走 ≤2 次带反馈修复；编辑/确认全部强制重校验；确认后写 16 位 plan_hash 并锁定。
  - `uv run pytest` → 41 passed：合法方案、幽灵字段二次修复、日期语义耗尽报错、未知算子拦截、编辑幽灵列拒绝、语义未确认 409。
- **Priority**: high
- **Depends On**: Task 2, Task 4, Task 5, Task 7
- **Description**:
  - LLM 将问题翻译为方案（步骤、参数、人读公式、结果形态、图表提示）；服务端强校验（算子白名单、字段存在、参数合法、日期约束）；失败走有限修复（≤2），耗尽返回结构化错误。
  - 方案确认接口（锁定 + 哈希）；编辑接口允许改指标/维度/时间粒度/筛选/TopN 后重新校验。
- **Acceptance Criteria Addressed**: AC-5, AC-15
- **Test Requirements**:
  - `rule` TR-9.1: 5 数据集 × 各至少 1 个录制方案通过校验且字段/算子合法；含 3 类非法固件（未知算子、幽灵字段、时间算子无日期）均被修复或拒绝；证据：pytest。
  - `rule` TR-9.2: 编辑后重校验通过并产生新哈希；确认接口返回锁定状态；证据：API 测试。
  - `rule` TR-9.3: 静态审计 planner/llm 层不存在任何代码执行能力（与 TR-4.2 合并执行一次全量审计）；证据：审计记录。

## Task 10: 计算引擎算子实现与图表映射

- **Status**: `completed`
- **Completion Evidence**:
  - `engine/ops.py` 纯 pandas 实现 10 算子，OpResult 含结果/人读公式/中间摘要；AST 守卫确保零 LLM 依赖；目录外算子双保险拒绝。
  - `uv run pytest` → 64 passed：全部算子手算黄金值（同环比增长率、占比合计=1、相关系数 ±1、IQR 哨兵）、错误中文阻断、附录 A 图表映射参数化一致。

## Task 11: Executor 执行器与计算台账

- **Status**: `completed`
- **Completion Evidence**:
  - `executor.py`：锁定+哈希双闸门、快照哈希复核、逐步台账（输入/输出行数/列/公式/摘要）、全量结果写 result.parquet、预览截断 1,000 行、失败不写台账。
  - `uv run pytest` → 69 passed：台账字段/数值手算一致、引擎错误中文阻断且无台账、未锁定/篡改哈希 409、20 万行 group_by 与 time_series 均 <5s。
- **Priority**: high
- **Depends On**: Task 4
- **Description**:
  - 以 pandas 实现 10 个算子（含筛选操作符集合、时间重采样日/周/月/季/年、同环比增长率、Pearson/Spearman、IQR/z-score 离群）；纯函数、输入 DataFrame + 参数 → 结果 DataFrame/标量 + 人读公式 + 中间摘要。
  - 末端算子 → 图表类型确定性映射（附录 A）。
  - 引擎包不得导入任何 LLM/HTTP 模块（import 守卫测试）。
- **Acceptance Criteria Addressed**: AC-6, AC-7
- **Test Requirements**:
  - `rule` TR-10.1: 每个算子至少 1 个手算黄金值单测（含同比百分比、占比合计、相关系数符号）；证据：pytest。
  - `rule` TR-10.2: 引擎目录 import 守卫：尝试导入 llm 客户端失败/静态扫描无 `from app.services.llm`；证据：测试。
  - `rule` TR-10.3: 10 个算子的图表映射与附录 A 完全一致；证据：参数化单测。

## Task 11: Executor 执行器与计算台账

- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 9, Task 10, Task 7
- **Description**:
  - 按锁定方案在快照上顺序执行；产出台账（会话/快照 ID 与哈希、参与行数、逐步记录、最终结果、图表规格、耗时）；结果表展示截断 1,000 行并注明全量行数；错误以中文阻断。
  - 性能：20 万行 × 常规聚合 ≤5s。
- **Acceptance Criteria Addressed**: AC-6, AC-7
- **Test Requirements**:
  - `rule` TR-11.1: 端到端执行多步方案，台账字段齐全、快照哈希与文件一致、最终数值与手算一致；证据：pytest。
  - `rule` TR-11.2: 字段缺失/类型不符返回中文错误且无结果台账写入；证据：负例单测。
  - `rule` TR-11.3: 合成 20 万行数据执行 group_by/time_series 断言耗时 ≤5s（M 系列 Mac）；证据：性能测试输出（允许标记为慢测试）。

## Task 12: 结果校验器 Validate

- **Status**: `completed`
- **Completion Evidence**:
  - `validator.py` 五项确定性检查：覆盖率（筛选可解释）、交叉验算（独立 pandas/numpy 重算：分组合计/占比和/增长率反推/相关系数复算/离群点数）、空值明示、形态（Inf 与越界占比 fail、时间缺口/空值 warn）、方案哈希一致性；warn 知悉闸门 `ensure_consumable`。
  - `uv run pytest` → 76 passed：正常 pass、筛选差额解释、占比交叉、结果篡改 fail 阻断、方案篡改 fail、时间缺口+空值 warn→知悉放行、fail 知悉 API 409。
- **Priority**: high
- **Depends On**: Task 11
- **Description**:
  - 五项确定性检查（覆盖率/交叉验算/空值处理/结果形态/方案哈希一致性），输出 pass/warn/fail + 说明 + 相关数字；fail 阻断接口；warn 需知悉确认；结果写入台账。
- **Acceptance Criteria Addressed**: AC-8, AC-U2
- **Test Requirements**:
  - `rule` TR-12.1: 构造 5 类场景（正常、筛掉行应被解释、分组和≠总计、意外 NaN、方案篡改）断言判定级别与阻断行为；证据：pytest。
  - `rule` TR-12.2: fail 时后续洞察/图表数据接口返回 409 类阻断状态；证据：API 测试。

## Task 13: Insight Generator

- **Status**: `completed`
- **Completion Evidence**:
  - LLM 输入仅含方案口径+≤50 行聚合摘要+校验状态（相关/离群只给统计摘要，无明细）；输出 2–5 条结构化洞察；门禁失败走 ≤2 次修复。
  - `uv run pytest` → 102 passed：结构完整、60 分组载荷截断 50 行、篡改数字 999 被拦截、二次修复成功。

## Task 14: Insight Validator（数字接地 + 因果护栏 + 可信度）

- **Status**: `completed`
- **Completion Evidence**:
  - 纯规则门禁：中英因果词表 12 正例/7 合规例（含免责语境豁免）、相关分析强制免责语、数字展示精度接地（日期/编号 token 剔除、百分比独立匹配）、证据步骤真实、增长率与相关系数方向校验。
  - 可信度服务端权威覆盖：无风险 high；相关/少周期/离群/warn 单风险 medium 封顶+需验证；多风险 low。相关洞察自动附带免责语。
  - 测试：相关分析缺免责语→修复→medium 封顶；负增长率误写上升被拦截；high/medium/low 策略三级覆盖。
- **Priority**: high
- **Depends On**: Task 2, Task 11, Task 12
- **Description**:
  - 输入仅含方案人读描述、聚合结果（展示精度）、校验状态、字典（无全量明细）；输出 2–5 条结构化洞察（类型、证据引用、数字清单、置信理由、需进一步验证标记）。
  - 生成后交洞察验证器门禁（Task 14），不通过则修复重生成 ≤2 次。
- **Acceptance Criteria Addressed**: AC-9, AC-15
- **Test Requirements**:
  - `rule` TR-13.1: 固件测试产出洞察结构完整、evidenceRefs 指向真实步骤/结果；证据：pytest。
  - `rule` TR-13.2: 提示词构造不含全量行级数据（断言输入载荷行数上限/仅聚合）；证据：单测检查 payload。

## Task 14: Insight Validator（数字接地 + 因果护栏 + 可信度）

- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 13
- **Description**:
  - 数字接地：文本数字按展示精度匹配引用结果，含方向/百分号规则；因果护栏词表（中英）拦截并要求改写，相关分析强制「相关关系，不代表因果」；证据完整性与统计方向一致性；可信度策略（high 条件与各类降级/封顶规则、low 标注「需进一步验证」）。
  - 提供可复用的拦截原因结构，供前端展示与修复循环。
- **Acceptance Criteria Addressed**: AC-6, AC-9, AC-10, AC-11, AC-12, AC-U2
- **Test Requirements**:
  - `rule` TR-14.1: ≥10 条因果正/负例（含中英词、合规豁免语境）判定全部正确，相关类结论必须带免责语；证据：pytest。
  - `rule` TR-14.2: 接地正/负例：篡改固件中一个数字必拦截，全部匹配通过；证据：pytest。
  - `rule` TR-14.3: 可信度策略表单测覆盖 high/medium/low 各触发条件（相关、小样本、离群、warn）；证据：pytest。

## Task 15: Follow-up 追问生成

- **Status**: `completed`
- **Completion Evidence**:
  - LLM 基于方案+结果摘要+字典生成 3–5 追问，后置校验数量/字段/算子/依据接地/无日期禁趋势。
  - `uv run pytest` → 106 passed：合法追问、无日期趋势拦截、空泛依据拦截、追问问题回流规划器成功产出合法品类下钻方案。
- **Priority**: medium
- **Depends On**: Task 14
- **Description**:
  - 基于方案与结果生成 3–5 条追问（下钻、粒度切换、相关指标、异常拆解），每条附依据与预填参数；接口回流到方案生成。
- **Acceptance Criteria Addressed**: AC-14
- **Test Requirements**:
  - `rule` TR-15.1: 契约测试数量 3–5、依据非空、引用字段合法；追问 payload 可成功调用规划接口；证据：pytest。

## Task 16: 示例数据集生成脚本与黄金值固件

- **Status**: `done`
- **Priority**: high
- **Depends On**: Task 3
- **Description**:
  - 固定随机种子脚本生成附录 B 的 5 份 CSV（2,000–6,000 行、规定时间跨度、中英混合表头），精确预埋质量问题并输出 `golden.json`（各类问题计数、若干手算/脚本算的聚合黄金值）；产物写入 `sample_data/` 与 `tests/fixtures/golden/`。
- **Acceptance Criteria Addressed**: AC-3, AC-13
- **Test Requirements**:
  - `rule` TR-16.1: 脚本可重复运行生成字节稳定文件（二次生成哈希一致）；golden.json 含每份数据的问题计数与 ≥3 个聚合黄金值；证据：命令输出 + 哈希比对。
- **Completion Evidence**:
  - `backend/scripts/generate_sample_data.py` 生成 5 份 CSV → 项目根 `sample_data/`（sales_orders 3004/operations_daily 174/marketing_campaigns 2164/inventory_movements 2163/customer_tickets 2600 原始行），黄金值 → `backend/tests/fixtures/golden/golden.json`（每份含 issue_ids/issue_counts/decisions/snapshot_rows/snapshot_hash/3–5 个黄金聚合值）。
  - 质量问题全部命中并断言：sales(格式/缺失/离群/重复各1)、ops(格式2/缺失2/离群1，含 -1 哨兵)、marketing(格式3/离群4/重复1，含 ¥ 货币串、220 行枚举大小写)、inventory(格式2/缺失1/离群1/重复1，负库存)、tickets(格式2/缺失1/离群1)。
  - 黄金值复用真实管线产出（SessionStore→parse→run_quality_checks→apply_decisions→engine.run_step），避免两套口径漂移。
  - TR-16.1 由 `backend/tests/test_sample_data_golden.py`（11 例）覆盖：builder 二次生成字节一致且与落盘 CSV 一致；以黄金决策复跑管线，snapshot_rows/snapshot_hash/golden_values 完全复现。

## Task 17: 全后端路由串联与离线端到端套件

- **Status**: `done`
- **Priority**: high
- **Depends On**: Task 8, Task 9, Task 12, Task 14, Task 15, Task 16
- **Description**:
  - 补齐 REST 路由覆盖十阶段（含阶段状态机、回退、错误态）；以 replay 固件在 5 份数据上跑完整闭环：上传→语义（模拟用户确认）→质量决策→推荐→选问题→方案确认→执行→校验→图表数据→洞察（含证据）→追问→回流再规划。
  - 录制脚本：live/record 模式一键录制全部固件（供有 key 时刷新）。
  - 畸形固件负例套件覆盖 5 个 AI 阶段的显式降级。
- **Acceptance Criteria Addressed**: AC-13, AC-15, AC-U3
- **Test Requirements**:
  - `rule` TR-17.1: `uv run pytest` 在无 key/禁网环境全绿，含 5 数据集闭环与黄金数值断言；证据：完整测试输出。
  - `rule` TR-17.2: 5 个阶段畸形固件均返回结构化 ContractError 与可重试语义，会话中无伪造结果；证据：pytest。
- **Completion Evidence**:
  - `backend/scripts/build_e2e_fixtures.py` 为 5 份数据产出 25 个提交版回放固件（`tests/fixtures/llm/{profile,questions,plan,insights,followup}/{stem}_*.json`）；每份 profile 固件预埋 1 个 medium 字段+候选（渠道/tickets/campaign/unit_cost/is_resolved）用于触发用户确认；insights 固件数字不手写，先在真实管线上执行规范方案并构建 ResultDigest 取展示精度值；固件二次生成聚合哈希一致（字节稳定）。
  - 5 个 LLM 服务的固件基础名统一为 `Path(source_sample or filename).stem`（profiler/recommender/planner/insight/followup），from-sample 会话即开即用。
  - `backend/tests/test_e2e_pipeline.py`（7 例）：TR-17.1 五数据集全闭环（语义确认→质量黄金决策→7 问题/≥4 分类→方案确认→执行→五项校验全 pass→2 条高置信接地洞察且无因果词→3 追问→8 类产物落盘），关键聚合与 golden.json 误差 <1e-4；TR-17.2 profile 非法枚举/questions 数量不足/plan 白名单外算子 run_sql/insights 数字 999999 未接地/followup 依据未接地，5 阶段全部抛 ContractError。
  - `backend/scripts/record_live_pipeline.py`：`LLM_FIXTURE_MODE=record` 一键录制单数据集全套固件（录制前清旧固件；warn 须知悉；fail 阻断终止并提示重录，禁止手改放行）。
  - 全量 `uv run pytest`：124 passed（无 key/无网络 replay）。

## Task 18: 前端骨架、上传页与会话工作流框架

- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 3, Task 17
- **Description**:
  - 全局极简设计基底（中性色 + 单一强调色、卡片、中文字体栈、统一间距）；API 客户端与类型；十阶段 Stepper（状态、可回退、闸门禁用态）；上传/拖拽页（限制提示、错误文案、5 个示例数据集一键载入）；会话刷新恢复。
- **Acceptance Criteria Addressed**: AC-1, AC-U1
- **Test Requirements**:
  - `rule` TR-18.1: `npm run build` 通过；上传合法文件进入语义阶段、非法文件显示中文原因；刷新后回到当前阶段；证据：构建输出 + 浏览器走查记录。
  - `rubric` TR-18.2: 视觉与导航清晰度；scale 1-5；anchors 1=堆砌混乱，3=可用但阶段感弱，5=克制统一、阶段与下一步始终明确；threshold ≥4；证据：截图 + 评审记录（实现者自评，最终以独立 Review 为准）。

## Task 19: 前端语义确认与数据质量面板

- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 18
- **Description**:
  - 数据字典表（类型/含义/单位/样例/置信度徽标）；中低置信字段标黄、候选单选/自定义/忽略的确认交互；未确认完禁用下一步。
  - 质量报告分组（四类），每条展示证据与建议动作；逐项决策控件；决策进度与快照生成/回退入口。
- **Acceptance Criteria Addressed**: AC-2, AC-3, AC-U2
- **Test Requirements**:
  - `rule` TR-19.1: 未确认字段存在时下一步禁用，全部确认后放行；质量问题逐条决策前继续禁用，决策后展示快照行数变化；证据：浏览器走查 + 截图。

## Task 20: 前端问题推荐与方案确认面板

- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 18
- **Description**:
  - 推荐问题按分类卡片展示，每条展开「推荐依据」（字段/特征/场景）；自定义输入与换一批；选中进入方案页。
  - 方案步骤卡（数据范围→筛选→分组/聚合→呈现），可编辑指标、维度、时间粒度、筛选、TopN；非法编辑给出内联错误；确认锁定态与方案哈希展示；契约失败错误态（重试/编辑）。
- **Acceptance Criteria Addressed**: AC-4, AC-5, AC-15, AC-U1
- **Test Requirements**:
  - `rule` TR-20.1: 走查：依据可见可展开；编辑触发重新校验且非法项内联报错；确认后步骤不可变并显示已锁定；证据：浏览器走查 + 截图。

## Task 21: 前端执行、校验与可视化面板

- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 18
- **Description**:
  - 执行加载态（展示「AI 不参与计算，pandas 引擎执行中」类阶段说明）；校验清单（pass/warn/fail 图标与说明，fail 阻断、warn 知悉勾选）；图表（Recharts 按图表规格渲染）+ 人读公式 + 数据范围 + 结果表（1,000 行截断提示）。
- **Acceptance Criteria Addressed**: AC-8, AC-U1, AC-U2
- **Test Requirements**:
  - `rule` TR-21.1: 正常结果图表/公式/范围/表格同屏；构造 fail 结果（或固件）时阻断且提示明确；warn 需勾选知悉；证据：浏览器走查 + 截图。

## Task 22: 前端洞察卡片、证据抽屉与追问

- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 18
- **Description**:
  - 洞察卡片（类型、置信度徽标、low「需进一步验证」、相关性免责语）；证据抽屉完整展示数据范围/字段/筛选/公式/结果值/校验状态并可定位台账步骤；追问卡片（依据可见）点击回流方案页并预填。
- **Acceptance Criteria Addressed**: AC-9, AC-12, AC-14, AC-U1, AC-U2
- **Test Requirements**:
  - `rule` TR-22.1: 走查：每条洞察六类证据齐备且数值与结果表一致；low 标签与免责语在构造用例中出现；追问点击成功带上下文进入方案页；证据：浏览器走查 + 截图。
  - `rubric` TR-22.2: 「问题→分析→图表→洞察→证据」主线突出度；scale 1-5；anchors 1=证据藏得深/主线断裂，3=可查但不突出，5=证据一触即达且叙事完整；threshold ≥4；证据：截图 + 评审记录。

## Task 23: 五数据集浏览器全链路走查与缺陷修复

- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 19, Task 20, Task 21, Task 22, Task 17
- **Description**:
  - 用浏览器自动化/手动对 5 份数据逐份走通十阶段，记录并修复全部阻断与关键体验缺陷；补齐文案、空态、加载态、错误态。
- **Acceptance Criteria Addressed**: AC-13, AC-U1
- **Test Requirements**:
  - `rule` TR-23.1: 5 份数据均产出至少 1 张图表与 1 条带证据洞察，无控制台关键错误；证据：每份关键阶段截图与走查记录。

## Task 24: 真 LLM 冒烟（需用户提供 key，可选）

- **Status**: `pending`
- **Priority**: low
- **Depends On**: Task 23
- **Description**:
  - 配置真实 DeepSeek key，在至少 1 份数据上 live 模式跑通完整闭环；录制/刷新固件；记录模型行为偏差并调整提示词/修复策略（不降低确定性校验标准）。
- **Acceptance Criteria Addressed**: AC-13
- **Test Requirements**:
  - `rule` TR-24.1: live 冒烟闭环完成，关键数值由引擎产出、洞察通过全部校验；若环境无 key 则记录为「用户待执行」，不阻塞交付；证据：冒烟记录或阻塞说明。

## Task 25: 重构 Phase 1 — AnalysisBundle 自动分析层（后端，规则版；不动前端）

- **Status**: `completed`（代码与测试完成；Superstore 实测 8/8 成功；**用户价值验收 Gate 待确认，确认前不进入 Phase 2**）
- **Priority**: high
- **Depends On**: Task 17（既有 10 算子引擎与质量快照）
- **Description**:
  - 从「单问题→单方案→单结果」升级为「Dataset → AnalysisBundle（4–8 个 AnalysisView）→ 批量执行 partial success」。
  - BundlePlanner 三阶段可替换架构：`CandidateGenerator`（穷举候选，允许冗余，frozen Candidate + family_key）→ `BundleSelector` Protocol（Phase 1 为 `RuleBundleSelector`，Phase 2 可无痛替换/增强为 LLM）→ `AnalysisBundle`。
  - 多样性规则：metric / dimension / analysis type 多样性 + 信息冗余铁律（同 dimension+metric 不得仅换 share/group_by/top_n 重复）；存在两个高价值指标（如 Sales+Profit）时强制产出「同维度跨指标对照」View（锚点优先 breakdown 维度、comparison 口径），余量填充顺序：对照 → relationship → anomaly → 第二指标趋势 → 第二指标新维度分布 → 第三指标总览，上限 8。
  - relationship 候选：字典 relations 优先；缺失时在快照上确定性计算指标对 pearson 相关（纯 pandas，含率/折扣类指标优先，|r|≥0.15 才采用）。
  - 执行内核抽取：`executor.execute_plan(snapshot, plan)` 纯函数，旧 `execute()` 闸门逻辑不变；`bundle_executor.execute_bundle` 逐 View 复用内核，单 View 任何异常收敛为 failed + 中文 reason，不拖垮其他 View。
  - View 级存储隔离：`storage/sessions/{sid}/bundle/bundle.json`、`execution.json`、`views/{view_id}/{result.parquet,ledger.json,validation.json}`；不触碰旧 artifact 槽位。
  - View 级校验：coverage / shape / null_handling；anomaly 预览为离群行优先（全量 parquet 不变）；相关系数/离群统计由算子 summary 透出。
  - API：`POST/GET /sessions/{id}/analysis-bundle`、`POST/GET .../analysis-bundle/execute`（阶段缺失 409、会话缺失 404）。
  - ChartSpec 延后 Phase 2，本阶段 chart 复用 ChartType 枚举；`ViewType.profitability` 已在契约占位。
- **Completion Evidence**:
  - 新增：`app/schemas/bundle.py`、`app/services/bundle_planner.py`、`app/services/bundle_executor.py`、`app/routers/bundle.py`、`scripts/run_bundle_demo.py`、3 个测试文件（32 例）。
  - 修改：`app/services/executor.py`（execute_plan 内核 + clean_rows 公开）、`app/services/storage.py`（bundle 隔离槽位）、`app/main.py`（路由注册）。
  - 全量 `uv run pytest -q`：**163 passed**（131 旧 + 32 新），零回归。
  - Superstore（tableau超市数据集.xls，10,194 行 ×21 列 → 质量快照 9,011 行）实测 8/8：Sales/Profit 双总览、Sales 月趋势、Segment×Sales、Category×Sales 占比、Customer Top5、Category×Profit 跨指标对照、Discount×Profit 相关（r≈-0.46）。
- **⚠️ Phase 2 第一优先级（用户明确要求，不可降级/遗忘）**:
  - **确定性 DerivedMetric / ratio 能力**：如 `Profit Margin = Profit / Sales`，由规则/引擎在数据快照上确定性计算，**禁止 LLM 直接计算比率**；产出 `profitability` ViewType（契约已占位），并让比率参与跨 View 洞察。
  - 随后才是 dashboard_synthesizer、跨 View Findings、完整 ChartSpec；前端改动属 Phase 3。
- **Acceptance Criteria Addressed**: AC-4, AC-5, AC-8, AC-13, AC-U1（为 Phase 2 跨视图洞察与 Dashboard 打底）
- **Test Requirements**:
  - `rule` TR-25.1: 5 份示例数据均产出 4–8 View、算子全在白名单、引用字段均存在、有日期必有 trend；证据：参数化测试通过。
  - `rule` TR-25.2: 同 (dim, metric) 零重复；同维度出现两次时指标必须不同；Sales+Profit 合成数据必须存在同维度对照；证据：planner 单测。
  - `rule` TR-25.3: 注入坏 View 时其余 View 成功（partial success），旧 ledger/result 槽位不被污染；证据：executor 单测。
  - `rule` TR-25.4: 用户验收 Gate——Superstore 实际 Bundle JSON + 每 View 真实计算结果经用户确认分析价值后，方可进入 Phase 2。

## Task 26: 重构 Phase 2 — 确定性派生比率 + DashboardArtifact + 跨视图 Findings（后端，不动前端）

- **Status**: `in_progress`（代码/测试/Superstore 实测完成；**用户验收 Gate 待确认，确认前不进入 Phase 3**）
- **Priority**: high
- **Depends On**: Task 25（AnalysisBundle 自动分析层）
- **Description**:
  - **第一优先级（用户明确要求）：确定性 DerivedMetric/ratio**——新增第 11 算子 `derive_ratio`（无轴单值/按 dimension/按 date+granularity），口径铁律 **Σnumerator/Σdenominator（先聚合后相除）**，分母 0/NaN 产出 null 不抛错；新增 `schemas/derived_metric.py`（DerivedMetricSpec）+ `services/derived_metrics.py`（规则注册表，第一版 profit_margin：利润/profit/净利/毛利 ÷ 销售|营收|revenue|sales|amount|收入；率/折扣命名字段不可自比；探测不到不产出）；**LLM 不参与比率定义与计算**。
  - Planner 槽位策略：探测到比率时生成 profitability 候选（锚点 breakdown→comparison，op=derive_ratio），**profitability 替换主指标 ranking 槽位**；同维度允许「2 原始指标 View + 1 派生比率 View」（派生不占原始冗余额度）；叙事顺序 结构→对照→利润率；VIEW_CHART[profitability] 占位 metric 改 bar；无比率数据集维持 ranking（Phase 1 行为零变化）。
  - 新增 `schemas/dashboard.py`（全部 extra=forbid）：ChartSpec（type/title/x_field/y_fields/dimension/metric/interactive）、KPI（value/change/change_type yoy·mom·wow·previous_period/unit）、DashboardSection 五段（overview/trend/structure/diagnosis/detail）、FilterDefinition、DrilldownFilter/DrillChild/DrilldownSuggestion、Finding（growth/decline/anomaly/risk/opportunity/structure/relationship + importance + evidence_view_ids + drilldown）、DashboardArtifact{bundle_id,title,kpis,sections,findings,risks,global_filters}。
  - 存储隔离：`bundle/dashboard.json`；view 级 `chart_spec.json`；探针 `bundle/probes/{probe_id}/{plan.json,result.parquet,ledger.json}`，**probe 预算 ≤3、异常全收敛不阻断 artifact**。
  - `dashboard_synthesizer.py`：KPI（Sales/Profit overview 值 + 利润率无轴 probe + trend 末两期 mom，不满足可比条件 change=null）；五段固定布局（只装成功 View，全部成功 View 可追溯）；ChartSpec 按 view.type+末端参数确定性生成并随 view 落盘（artifact 持 view_id 索引）；global_filters 取 ≤30 基数主维度+快照真实成员。
  - `dashboard_insight.py` 四类纯规则检测器（中文模板强制四要素：现象/位置/量化/建议；FindingsDetector Protocol 为 LLM 增强预留，本阶段不实现）：① divergence 量利背离（利润份额−销售份额 ≤−10pp 或利润为负，evidence 双 view，负成员→high）；② risk.negative_member（负利润成员 + filter→group_by 下一层级维度 drilldown probe，层级语义优先「子/sub/商品」类，Top2 负贡献子成员，probe 失败 drilldown=None 但 finding 保留）；③ relationship（|r|≥0.3，负相关→risk 且率/折扣类指标作驱动因素，固定因果免责语）；④ trend（末两期 |变化|≥10% → growth/decline）。
  - API：`POST/GET /sessions/{id}/dashboard`（bundle/execution 缺失 409、会话 404，门禁同 bundle）；main.py 注册；Phase 1 与旧十阶段接口行为不变；前端零改动。
- **Completion Evidence**:
  - 新增：`app/schemas/derived_metric.py`、`app/schemas/dashboard.py`、`app/services/derived_metrics.py`、`app/services/dashboard_synthesizer.py`、`app/services/dashboard_insight.py`、`app/routers/dashboard.py`、`scripts/run_dashboard_demo.py`、4 个测试文件。
  - 修改：`schemas/plan.py`（DeriveRatioParams/Step + Union 第 11 个 + 轴互斥校验）、`engine/ops.py`（op_derive_ratio）、`engine/catalog.py`（OP_META/step_columns/validate）、`offline_fallback.py`（_step/_SHAPE_ZH）、`bundle_planner.py`（profitability 候选+槽位替换）、`schemas/bundle.py`（profitability→bar）、`storage.py`（dashboard/probe/chart_spec 槽位）、`main.py`。
  - 全量 `uv run pytest -q`：**184 passed**（基线 163 + 新增 21：derive_ratio 6、derived_metrics 7、planner profitability 1、dashboard 管线 3、dashboard API 3、图表映射 1），零回归。
  - Superstore 实测（快照 9,011 行）：KPI Sales=833,008.31（mom +0.2%）、Profit=101,848.47、利润率=**12.23%**；Category 利润率 Office Supplies 19.5%/Technology 15.5%/**Furniture −0.8%**；3 条 Findings——量利背离（Furniture 销售份额 30.4% vs 利润份额 −2.0%，缺口 32.4pp）、亏损风险（Furniture −2,054，drilldown→Sub-Category：**Tables −8,611、Bookcases −4,853**）、Discount×Profit 负相关（r=−0.4575，含因果免责语）；探针 2/3。
- **Acceptance Criteria Addressed**: AC-4, AC-5, AC-8, AC-13, AC-U1
- **Test Requirements**:
  - `rule` TR-26.1: derive_ratio 口径铁律——构造组间规模悬殊数据，断言 Σnum/Σden ≠ 行级比率均值（符号可相反）；分母 0→null 不抛；维度 null 成员排末位；参数轴互斥校验；证据：test_engine_ops.py。
  - `rule` TR-26.2: profit_margin 规则探测命中（Sales+Profit/中文命名/净利优先/销售额优先于金额），率命名字段不自比，无利润或无收入字段不命中；证据：test_derived_metrics.py。
  - `rule` TR-26.3: profitability 替换 ranking 槽位（derive_ratio 参数正确、同维第 3 视角、无比率数据集 ranking 保留）；证据：test_bundle_planner.py。
  - `rule` TR-26.4: 真实管线合成 DashboardArtifact——五段齐全且成功 View 全可追溯、margin KPI=ΣProfit/ΣSales、背离/负成员+drilldown（Tables 类子成员）/负相关+免责语、risks 子集、probe ≤3 隔离；证据：test_dashboard_pipeline.py。
  - `rule` TR-26.5: API 门禁 409/404、POST 合成契约可解析、GET 恢复；证据：test_dashboard_api.py。
  - `rule` TR-26.6: 用户验收 Gate——Superstore 实际 DashboardArtifact JSON + KPI/Sections/Findings 摘要经用户确认后，方可进入 Phase 3（前端）。
