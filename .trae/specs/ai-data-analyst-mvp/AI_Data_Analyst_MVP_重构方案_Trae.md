# AI Data Analyst MVP 重构方案

> 目标仓库：`zhuoyi1212/ai-data-analyst-mvp`  
> 目标：将当前“单问题 → 单方案 → 单图 → 单次洞察”的引导式分析器，重构为“上传数据 → AI 自动多维分析 → 多视图 Dashboard → 联动筛选 / 下钻 / AI 追问”的数据分析工作台。

---

## 1. 本次重构的核心结论

当前项目的主要问题不是图表样式不够好看，而是产品架构本身限制了分析深度。

当前链路：

```text
上传数据
→ 字段识别
→ 数据质量
→ AI 推荐问题
→ 用户选择 1 个问题
→ 生成 1 个 AnalysisPlan
→ 执行 1 组算子
→ 生成 1 张图
→ 生成 2~5 条局部洞察
```

这会天然导致：

- 每次只能回答一个问题；
- 每次只能得到一个分析方向；
- 图表之间无法组合；
- AI 只能对局部结果做总结；
- 无法形成“经营总览 / 趋势 / 结构 / 异常 / 盈利诊断”等完整分析；
- 用户必须先理解“自己应该问什么”，而不是系统主动发现问题；
- 前端更像流程表单，不像真正的数据分析工作台。

新版必须改为：

```text
上传数据
→ 数据理解
→ AI 自动生成分析蓝图
→ 同时生成多个分析任务
→ 批量执行
→ 跨结果综合洞察
→ 自动生成 Dashboard
→ 全局筛选 / 图表联动 / 下钻
→ AI 继续追问
```

核心原则：

> 不再让用户先“选一个分析方向”，而是系统自动完成一轮完整的数据分析，再让用户基于结果继续探索。

---

# 2. 新产品定位

旧定位：

> AI 帮不会 SQL 的用户回答一个数据问题。

新定位：

> 上传业务数据，AI 自动识别值得分析的指标和维度，执行多维分析，生成可交互 Dashboard，并持续发现异常、差异与值得继续追问的问题。

---

# 3. 新前端主链路

新版前端不再以 7 步 Stepper 作为核心交互。

建议改为：

```text
数据工作台
↓
① 上传数据
↓
② 数据理解
↓
③ AI 自动分析
↓
④ Dashboard
↓
⑤ 交互探索
↓
⑥ AI 下钻 / 追问
↓
⑦ 保存结果 / 导出
```

用户真正需要频繁使用的是：

```text
Dashboard
↕
筛选 / 联动 / 下钻
↕
AI 分析助手
```

---

# 4. 页面信息架构

建议最终保留 4 类主要页面。

## 4.1 数据工作台

用途：

- 上传 CSV / Excel；
- 查看最近使用的数据集；
- 查看历史分析；
- 重新打开已生成 Dashboard。

页面建议：

```text
AI Data Analyst

[ 上传数据 ]

最近数据集
────────────────
Superstore.csv
Orders.xlsx
Marketing.csv

最近分析
────────────────
Superstore 销售分析
Marketing ROI 分析
```

不要在首页展示复杂的数据质量流程。

---

## 4.2 数据理解页

数据上传完成后进入。

目标：

> 让系统自动完成大部分判断，只要求用户确认真正存在歧义的内容。

页面包含：

### 数据摘要

```text
9,994 行
21 个字段
时间范围：2022-01 ~ 2025-12
```

### 自动识别字段

```text
Sales           指标
Profit          指标
Discount        指标
Order Date      日期
Region          地区
Category        分类
Customer Name   客户
```

### 数据质量

只突出真正影响分析的问题：

```text
⚠ Profit 缺失 2.3%
⚠ Order Date 有 14 条无法识别
✓ 其余字段可直接分析
```

用户确认后：

```text
[ 开始 AI 自动分析 ]
```

不要再让用户进入：

```text
推荐问题 → 选择问题
```

---

# 5. AI 自动分析蓝图

数据理解完成后，系统自动生成：

```text
AnalysisBundle
```

替代当前：

```text
AnalysisPlan
```

一个 Bundle 内包含多个分析视角。

推荐默认分析框架：

1. 总体表现 Overview
2. 时间趋势 Trend
3. 结构拆解 Breakdown
4. 对比分析 Comparison
5. 盈利 / 效率 Profitability
6. 指标关系 Relationship
7. 异常与风险 Anomaly

注意：

并不是所有数据集都强制生成 7 类。

系统应根据字段决定是否启用。

示例：

```text
存在日期字段
→ Trend

存在分类字段
→ Breakdown / Ranking

存在地区字段
→ Geography / Region Comparison

存在多个连续数值指标
→ Relationship

存在利润/成本/收入类字段
→ Profitability

存在足够样本
→ Outlier / Anomaly
```

---

# 6. 新后端核心数据结构

## 6.1 AnalysisBundle

新增：

```python
class AnalysisBundle(BaseModel):
    bundle_id: str
    title: str
    primary_metrics: list[str]
    primary_dimensions: list[str]
    analysis_views: list[AnalysisView]
```

---

## 6.2 AnalysisView

```python
class AnalysisView(BaseModel):
    view_id: str
    title: str

    type: Literal[
        "overview",
        "trend",
        "breakdown",
        "comparison",
        "profitability",
        "relationship",
        "anomaly",
        "ranking"
    ]

    question: str

    plan: AnalysisPlan

    chart_spec: ChartSpec | None

    priority: int = 0
```

其中：

- `AnalysisPlan` 可以继续复用当前已有结构；
- 当前 10 个白名单算子仍然保留；
- 一个 `AnalysisView` 对应一个独立分析任务；
- 一个 `AnalysisBundle` 同时包含多个 `AnalysisView`。

---

# 7. 现有 10 个分析算子继续保留

当前算子：

```text
filter
aggregate
group_by
share
top_n
time_series
period_compare
compare_groups
correlation
outlier_flag
```

不需要推翻。

当前真正的问题不是计算能力，而是：

> 系统一次只允许使用这些算子回答一个问题。

新版改成：

```text
AnalysisBundle
 ├─ View A → time_series
 ├─ View B → group_by
 ├─ View C → top_n
 ├─ View D → share
 ├─ View E → correlation
 └─ View F → outlier_flag
```

批量执行。

---

# 8. 新 Analysis Planner

当前文件：

```text
backend/app/services/planner.py
```

当前目标：

> 把一个用户问题转成一个 AnalysisPlan。

需要新增一个上层 Planner：

```text
backend/app/services/bundle_planner.py
```

职责：

```text
DataDictionary
+
Data Profile
+
Quality Result
↓
自动判断核心指标
↓
自动判断核心维度
↓
自动生成 5~10 个 AnalysisView
↓
每个 View 生成独立 AnalysisPlan
↓
组成 AnalysisBundle
```

---

## 8.1 Bundle Planner 输出示例

Superstore：

```json
{
  "title": "Superstore Sales Analysis",
  "primary_metrics": [
    "Sales",
    "Profit",
    "Quantity",
    "Discount"
  ],
  "primary_dimensions": [
    "Region",
    "Category",
    "Sub-Category",
    "Segment"
  ],
  "analysis_views": [
    {
      "view_id": "sales_overview",
      "type": "overview",
      "title": "总体经营表现",
      "question": "销售、利润和订单整体表现如何"
    },
    {
      "view_id": "sales_trend",
      "type": "trend",
      "title": "销售与利润趋势",
      "question": "销售额和利润如何随时间变化"
    },
    {
      "view_id": "category_performance",
      "type": "breakdown",
      "title": "品类表现",
      "question": "不同品类的销售和利润贡献如何"
    },
    {
      "view_id": "regional_performance",
      "type": "comparison",
      "title": "区域表现",
      "question": "不同区域的表现有什么差异"
    },
    {
      "view_id": "discount_profit",
      "type": "relationship",
      "title": "折扣与利润",
      "question": "折扣与利润是否存在明显关系"
    },
    {
      "view_id": "profit_anomaly",
      "type": "anomaly",
      "title": "亏损与异常",
      "question": "哪些订单或组合存在异常亏损"
    }
  ]
}
```

---

# 9. 批量执行器

新增：

```text
backend/app/services/bundle_executor.py
```

职责：

```text
for view in bundle.analysis_views:
    execute(view.plan)
    validate(result)
    save(result)
```

要求：

- 每个 view 独立执行；
- 一个 view 失败不能导致整个 Dashboard 全部失败；
- 返回 view-level 状态；
- 支持 partial success。

示例：

```json
{
  "bundle_id": "...",
  "views": [
    {
      "view_id": "sales_trend",
      "status": "success"
    },
    {
      "view_id": "correlation",
      "status": "failed",
      "reason": "有效样本不足"
    }
  ]
}
```

---

# 10. Dashboard 数据结构

建议新增：

```python
class DashboardArtifact(BaseModel):
    bundle_id: str
    title: str

    kpis: list[KPI]
    sections: list[DashboardSection]

    findings: list[Finding]
    risks: list[Finding]

    global_filters: list[FilterDefinition]
```

---

## 10.1 KPI

```python
class KPI(BaseModel):
    label: str
    value: float | str
    change: float | None
    change_type: Literal["yoy", "mom", "wow", "previous_period"] | None
    unit: str | None
```

---

## 10.2 DashboardSection

```python
class DashboardSection(BaseModel):
    section_id: str
    title: str
    view_ids: list[str]
```

建议默认：

```text
overview
trend
structure
diagnosis
detail
```

---

# 11. Dashboard 页面结构

这是新版最核心的页面。

整体结构：

```text
┌──────────────────────────────────────────────────────────┐
│ 数据集名称                     全局筛选       AI重新分析 │
├──────────────┬──────────────────────────────┬────────────┤
│              │                              │            │
│ 分析导航      │          Dashboard           │ AI助手      │
│              │                              │            │
│ 总览          │ KPI KPI KPI KPI              │ 关键发现    │
│ 趋势          │                              │            │
│ 结构          │ 趋势图       区域图           │ 异常提醒    │
│ 盈利诊断      │                              │            │
│ 异常          │ 品类图       盈利图           │ 推荐追问    │
│ 数据明细      │                              │            │
│              │                              │            │
└──────────────┴──────────────────────────────┴────────────┘
```

---

# 12. Dashboard 顶部

顶部包含：

```text
Superstore Sales Dashboard

时间：2024
区域：全部
品类：全部
客户类型：全部

[ 重置 ] [ 导出 ]
```

全局筛选改变后：

```text
所有图表
+
KPI
+
AI洞察
```

同步更新。

---

# 13. Dashboard KPI 区

建议 3~6 个指标。

例如：

```text
销售额
$2.30M
↑ 12.8%

利润
$286K
↓ 4.2%

利润率
12.4%
↓ 2.1pp

订单数
5,009
↑ 8.7%
```

不要仅展示绝对值。

优先展示：

```text
当前值
+
与上一周期的变化
```

---

# 14. Dashboard 图表区

Dashboard 不等于无限堆图。

建议总览页：

```text
4 KPI
+
4~6 张主要图
+
AI Findings
```

---

## 14.1 默认布局示例

```text
KPI KPI KPI KPI

Sales / Profit Trend
─────────────────────

Region Performance   Category Performance
─────────────────   ─────────────────────

Profitability        Anomaly
──────────────       ─────────────
```

---

# 15. 分析页签

左侧导航：

```text
总览
趋势分析
结构分析
盈利诊断
异常发现
数据明细
```

---

## 15.1 总览

回答：

> 这份数据整体发生了什么？

包含：

- 核心 KPI；
- 核心趋势；
- 主要贡献者；
- 主要风险；
- Top Findings。

---

## 15.2 趋势分析

包含：

- 时间趋势；
- 同比 / 环比；
- 拐点；
- 异常月份；
- 峰值 / 低谷。

---

## 15.3 结构分析

包含：

- Category；
- Region；
- Segment；
- Customer；
- 贡献占比；
- Top N。

---

## 15.4 盈利诊断

目标：

不是单纯展示 Profit。

而是：

```text
Profit ↓
↓
哪个 Category？
↓
哪个 Sub-Category？
↓
哪个 Region？
↓
哪些订单？
↓
共同特征是什么？
```

---

## 15.5 异常发现

包含：

- 离群订单；
- 异常月份；
- 异常组合；
- 突然变化；
- 高收入低利润组合；
- 高折扣亏损组合。

---

# 16. AI 洞察系统重构

当前：

```text
backend/app/services/insight.py
```

当前只能对：

```text
单个 AnalysisPlan 的结果
```

写洞察。

必须新增：

```text
backend/app/services/dashboard_insight.py
```

职责：

```text
多个 View Result
↓
跨视图分析
↓
寻找一致趋势
↓
寻找指标背离
↓
定位贡献来源
↓
寻找异常组合
↓
生成 Key Findings
```

---

# 17. 洞察不再只是描述图表

禁止输出这种无价值内容：

```text
Technology 销售额最高。
West 区域销售额较高。
Furniture 利润较低。
```

应该形成分析链：

```text
现象
↓
差异
↓
定位
↓
拆解
↓
验证
↓
建议继续分析
```

---

# 18. 高价值洞察示例

例如：

```text
销售额同比增长 12.8%，但利润同比下降 4.2%，
销售增长没有同步转化为利润增长。
```

继续定位：

```text
利润压力主要集中在：

Central
×
Furniture
×
Tables
```

进一步：

```text
该组合销售规模较高，但利润贡献为负，
同时高折扣订单占比明显更高。
```

最后：

```text
Discount 与 Profit 在当前样本中呈负相关。

该结果仅代表数据关联，
不能直接推断折扣导致利润下降。
```

---

# 19. Findings 数据结构

建议：

```python
class Finding(BaseModel):
    finding_id: str

    title: str
    summary: str

    type: Literal[
        "growth",
        "decline",
        "anomaly",
        "risk",
        "opportunity",
        "structure",
        "relationship"
    ]

    evidence_view_ids: list[str]

    importance: Literal[
        "high",
        "medium",
        "low"
    ]

    drilldown: DrilldownSuggestion | None
```

---

# 20. 自动下钻

这是新版核心能力。

当系统发现：

```text
Profit ↓
```

自动生成 Drilldown：

```text
Category
↓
Sub-Category
↓
Region
↓
Segment
```

找到：

```text
Central
×
Furniture
×
Tables
```

用户点击 Finding：

```text
利润下降主要集中在 Central / Furniture / Tables
```

前端自动应用筛选：

```text
Region = Central
Category = Furniture
Sub-Category = Tables
```

Dashboard 全局更新。

---

# 21. 图表联动

需要学习 Tableau 的核心体验：

> 一个图可以成为另一个图的筛选器。

例如用户点击：

```text
West
```

全局状态变为：

```text
Region = West
```

以下同步刷新：

```text
KPI
趋势
Category
Sub-Category
客户
AI洞察
```

再点击：

```text
Furniture
```

过滤变为：

```text
Region = West
Category = Furniture
```

---

# 22. 前端状态模型

建议新增统一 Dashboard State：

```typescript
interface DashboardState {
  sessionId: string;

  filters: {
    [field: string]: string[] | number[] | DateRange;
  };

  activeSection: string;

  selectedView?: string;

  selectedFinding?: string;
}
```

所有图表读取统一 filters。

---

# 23. 前端组件重构

当前：

```text
frontend/src/components/panels/
```

包含：

```text
ProfilePanel
QualityPanel
QuestionsPanel
PlanPanel
RunPanel
InsightPanel
```

新版不应该继续以 Panel 为主结构。

建议新增：

```text
frontend/src/components/dashboard/
```

---

## 23.1 新组件建议

```text
DashboardShell.tsx
DashboardHeader.tsx
DashboardSidebar.tsx
GlobalFilters.tsx

KPIGrid.tsx
KPICard.tsx

DashboardGrid.tsx
ChartCard.tsx

AIInsightPanel.tsx
FindingCard.tsx

DrilldownDrawer.tsx

DataTable.tsx
```

---

# 24. 图表组件

新增：

```text
frontend/src/components/charts/
```

例如：

```text
LineChart.tsx
BarChart.tsx
StackedBarChart.tsx
ScatterChart.tsx
AreaChart.tsx
PieChart.tsx
```

如果已有 Recharts，可以继续使用。

不要让后端直接返回 React 组件。

后端只返回：

```json
{
  "type": "line",
  "x": "month",
  "series": [
    {
      "field": "sales",
      "label": "Sales"
    },
    {
      "field": "profit",
      "label": "Profit"
    }
  ]
}
```

前端根据 chart spec 渲染。

---

# 25. ChartSpec

建议：

```typescript
interface ChartSpec {
  type:
    | "line"
    | "bar"
    | "stacked_bar"
    | "area"
    | "scatter"
    | "pie";

  title: string;

  xField?: string;

  yFields?: string[];

  dimension?: string;

  metric?: string;

  interactive?: boolean;
}
```

---

# 26. 当前前端需要删除 / 降级的内容

以下内容不应该继续作为主流程：

## QuestionsPanel

当前：

```text
选择你想分析的问题
```

改为：

```text
推荐追问
```

放到 Dashboard 右侧 AI Assistant。

---

## PlanPanel

普通用户不再需要看到：

```text
aggregate
group_by
time_series
period_compare
```

AnalysisPlan 继续存在于后端。

前端只允许在：

```text
高级模式 / 查看分析逻辑
```

查看。

---

## RunPanel

执行过程不应该占一个主页面。

改成：

```text
AI 正在分析数据

✓ 总体表现
✓ 趋势
✓ 品类
✓ 区域
● 盈利诊断
○ 异常分析
```

分析完成直接进入 Dashboard。

---

## InsightPanel

不再单独一个页面。

改为：

```text
Dashboard 右侧 AIInsightPanel
```

---

# 27. Workflow 重构

当前文件：

```text
frontend/src/lib/workflow.ts
```

当前：

```text
upload
profile
quality
questions
plan
run
insight
```

建议改为：

```text
upload
understand
analyzing
dashboard
```

例如：

```typescript
export const WORKFLOW_STEPS = [
  {
    key: "upload",
    label: "上传数据"
  },
  {
    key: "understand",
    label: "数据理解"
  },
  {
    key: "analyzing",
    label: "AI 分析"
  },
  {
    key: "dashboard",
    label: "Dashboard"
  }
];
```

注意：

Dashboard 不是 Stepper 的“最后一步”。

Dashboard 是产品主要工作区。

---

# 28. 路由建议

当前：

```text
/
sessions/[id]
```

建议继续保留 session，但增加：

```text
/
    数据工作台

/sessions/[id]/understand
    数据理解

/sessions/[id]/analyzing
    AI 自动分析

/sessions/[id]/dashboard
    Dashboard
```

---

# 29. API 重构

建议新增：

```text
POST
/api/sessions/{id}/analysis-bundle
```

生成：

```text
AnalysisBundle
```

---

```text
POST
/api/sessions/{id}/analysis-bundle/execute
```

批量执行。

---

```text
GET
/api/sessions/{id}/dashboard
```

获取完整 Dashboard 数据。

---

```text
POST
/api/sessions/{id}/dashboard/filter
```

基于筛选重新计算。

---

```text
POST
/api/sessions/{id}/dashboard/ask
```

用户向 AI 追问。

---

# 30. AI 追问

右侧始终保留：

```text
AI 分析助手
```

用户可以问：

```text
为什么利润下降？

哪个区域问题最大？

Furniture 为什么亏损？

折扣是否影响利润？

哪些客户值得重点关注？
```

---

# 31. AI 追问执行方式

不要只让 LLM 直接回答。

流程：

```text
用户问题
↓
Question Planner
↓
生成 AnalysisPlan
↓
确定性执行
↓
结果校验
↓
生成回答
↓
必要时新增 Dashboard View
```

例如用户问：

```text
为什么利润下降？
```

AI 可以自动执行：

```text
按 Category 拆
↓
按 Sub-Category 拆
↓
按 Region 拆
↓
分析 Discount
```

最终形成新的 AnalysisView：

```text
profit_decline_diagnosis
```

添加到 Dashboard。

---

# 32. 数据可信原则继续保留

当前项目有一个很有价值的方向：

```text
LLM 不直接计算数字
+
确定性计算
+
结果校验
+
证据链
```

这一部分不要删除。

新版依然坚持：

```text
LLM
负责：

选择分析方式
生成分析问题
解释结果

Python / Pandas
负责：

计算
聚合
排序
同比环比
相关性
异常检测
```

---

# 33. 证据链降级为辅助能力

不要在 Dashboard 主界面展示大量：

```text
step_id
plan_hash
snapshot_hash
formula
```

普通用户不关心。

改成：

```text
查看分析依据
```

点击后 Drawer 展示：

```text
数据范围
计算口径
使用字段
样本数量
分析步骤
```

---

# 34. Dashboard Synthesizer

新增：

```text
backend/app/services/dashboard_synthesizer.py
```

输入：

```text
AnalysisBundle
+
所有 View Results
```

输出：

```text
DashboardArtifact
```

职责：

1. 选择核心 KPI；
2. 决定图表优先级；
3. 组织 Dashboard Section；
4. 生成全局 Findings；
5. 推荐 Drilldown；
6. 推荐全局 Filter。

---

# 35. Dashboard 不应由 LLM 完全决定

建议：

```text
规则系统
+
LLM
```

共同完成。

规则负责：

```text
时间 → line
分类比较 → bar
占比 → stacked bar / pie
相关性 → scatter
KPI → KPI card
```

LLM 只负责：

```text
选择哪些分析值得展示
+
标题
+
排序
+
洞察
```

---

# 36. MVP 第一版 Dashboard 范围

不要第一版做得过重。

先支持：

### KPI

```text
sum
count
mean
change
```

### 图表

```text
Line
Bar
Stacked Bar
Scatter
```

第一版暂时不要优先：

```text
复杂地图
桑基图
雷达图
3D 图
自定义 Vega
```

---

# 37. 推荐第一版固定生成 6~8 个 View

如果数据条件允许：

```text
1 Overall KPI
2 Main Trend
3 Dimension Ranking
4 Dimension Contribution
5 Group Comparison
6 Metric Relationship
7 Anomaly
8 Profitability / Efficiency
```

不用让用户提前选择。

---

# 38. Loading 页面也需要重新设计

当前不要只显示 Spinner。

建议：

```text
AI 正在分析这份数据

✓ 识别 4 个核心指标
✓ 识别 5 个主要维度
✓ 生成总体分析
✓ 分析时间趋势
✓ 分析品类结构
● 定位异常和风险
○ 生成 Dashboard

已完成 6 / 8
```

用户可以理解系统正在做什么。

---

# 39. Dashboard 空状态

如果数据不足：

不要报：

```text
该算子无法执行
```

用户不应该看到内部实现。

显示：

```text
暂未生成相关性分析

原因：
有效连续数值字段不足。
```

---

# 40. 设计风格

整体采用：

```text
极简
数据密度高
低装饰
低饱和色
```

避免：

- 渐变背景；
- 大量彩色卡片；
- 每个模块都不同颜色；
- 花哨 icon；
- 大面积视觉装饰。

建议：

```text
白 / 灰
+
1 个主强调色
+
图表色作为数据编码
```

---

# 41. 前端布局建议

桌面端：

```text
Sidebar 180px
+
Main Dashboard 自适应
+
AI Panel 280~320px
```

Dashboard 内容最大宽度不要限制得太窄。

建议：

```text
min-width: 0
width: 100%
```

---

# 42. 图表 Card 设计

每张 Chart Card：

```text
标题
一句副标题 / 指标定义

图表

关键结论
```

例如：

```text
销售额与利润趋势

按月统计

[图表]

8 月销售达到峰值，但利润率同步下降。
```

不要把解释完全放到右侧 AI Panel。

每张图本身也应该有一个局部结论。

---

# 43. 右侧 AI Panel

右侧结构：

```text
AI 分析

关键发现
──────────
01 销售增长但利润下降

02 利润压力集中在
Central / Furniture

03 高折扣订单值得关注

──────────

建议继续分析

[ 为什么利润下降？ ]

[ 哪些客户贡献最大？ ]

[ 查看异常订单 ]

──────────

输入框
问问这份数据...
```

---

# 44. Finding 与 Dashboard 联动

点击 Finding：

```text
Central / Furniture / Tables
```

自动：

```text
应用过滤条件
+
高亮相关图表
+
滚动到相关 View
```

---

# 45. 数据明细

保留一个：

```text
数据明细
```

页面。

支持：

- 排序；
- 筛选；
- 查看被过滤后的原始记录；
- 点击某条记录查看更多字段。

但不要成为默认首页。

---

# 46. 原有代码的迁移策略

不要一次性推翻全部代码。

分阶段改造。

---

# Phase 1：保留执行引擎，新增 Bundle 层

保留：

```text
backend/app/services/engine/
backend/app/services/executor.py
backend/app/services/validator.py
backend/app/services/profiler.py
backend/app/services/quality_checker.py
```

新增：

```text
schemas/bundle.py
services/bundle_planner.py
services/bundle_executor.py
```

目标：

一个 session 可以同时执行多个 AnalysisPlan。

---

# Phase 2：生成 Dashboard Artifact

新增：

```text
schemas/dashboard.py
services/dashboard_synthesizer.py
services/dashboard_insight.py
```

能够输出：

```text
KPI
Views
Sections
Findings
Filters
```

---

# Phase 3：重构前端

新增：

```text
components/dashboard/
components/charts/
```

创建：

```text
DashboardShell
DashboardSidebar
DashboardHeader
KPIGrid
DashboardGrid
AIInsightPanel
GlobalFilters
```

---

# Phase 4：替换旧主流程

逐步降低：

```text
QuestionsPanel
PlanPanel
RunPanel
InsightPanel
```

的重要性。

最终：

```text
QuestionsPanel
→ 推荐追问

PlanPanel
→ 高级查看

RunPanel
→ 分析进度

InsightPanel
→ AIInsightPanel
```

---

# Phase 5：加入交互分析

加入：

```text
Global Filter
Chart Click Filter
Finding Drilldown
AI Follow-up
```

---

# 47. 后端目录建议

最终建议：

```text
backend/app/

schemas/
  bundle.py
  dashboard.py
  plan.py
  insight.py

services/

  profiler.py
  quality_checker.py

  planner.py

  bundle_planner.py
  bundle_executor.py

  executor.py
  validator.py

  dashboard_synthesizer.py
  dashboard_insight.py

  followup.py

  engine/
```

---

# 48. 前端目录建议

```text
frontend/src/

app/
  page.tsx

  sessions/
    [id]/
      understand/
        page.tsx

      analyzing/
        page.tsx

      dashboard/
        page.tsx

components/

  dashboard/
    DashboardShell.tsx
    DashboardHeader.tsx
    DashboardSidebar.tsx
    DashboardGrid.tsx
    KPIGrid.tsx
    KPICard.tsx
    GlobalFilters.tsx
    AIInsightPanel.tsx
    FindingCard.tsx
    DrilldownDrawer.tsx

  charts/
    LineChart.tsx
    BarChart.tsx
    StackedBarChart.tsx
    ScatterChart.tsx

  data/
    DataPreview.tsx
    DataTable.tsx

lib/
  api.ts
  dashboard.ts
  types.ts
```

---

# 49. 首要改造文件

当前重点检查和修改：

```text
backend/app/services/planner.py
backend/app/services/recommender.py
backend/app/services/insight.py

backend/app/schemas/plan.py

frontend/src/components/panels/QuestionsPanel.tsx
frontend/src/components/panels/PlanPanel.tsx
frontend/src/components/panels/RunPanel.tsx
frontend/src/components/panels/InsightPanel.tsx

frontend/src/lib/workflow.ts
frontend/src/lib/types.ts
frontend/src/lib/api.ts
```

注意：

不要直接把这些文件全部删掉。

先新增新架构，再逐步迁移。

---

# 50. Trae 实现顺序

请严格按照以下顺序开发。

## Step 1

先阅读整个仓库。

重点理解：

```text
Session
Dictionary
Plan
Executor
Ledger
Validation
Insight
```

不要直接开始改 UI。

---

## Step 2

确认现有执行引擎：

```text
AnalysisPlan
→ Executor
→ Ledger
→ Validation
```

能够被多次调用。

---

## Step 3

实现：

```text
AnalysisBundle
AnalysisView
```

---

## Step 4

实现：

```text
BundlePlanner
```

自动生成 5~10 个分析 View。

---

## Step 5

实现：

```text
BundleExecutor
```

批量执行。

---

## Step 6

实现：

```text
DashboardSynthesizer
```

输出统一 Dashboard JSON。

---

## Step 7

新增：

```text
/dashboard
```

页面。

先使用真实后端 JSON 渲染。

不要先做 Mock Dashboard 再长期不接数据。

---

## Step 8

完成：

```text
KPI
Line
Bar
Scatter
```

四类基础组件。

---

## Step 9

加入：

```text
GlobalFilters
```

---

## Step 10

实现：

```text
Dashboard Insight
```

---

## Step 11

将：

```text
Questions / Plan / Run / Insight
```

逐步从主流程删除。

---

# 51. 第一阶段验收标准

上传 Superstore CSV 后：

用户不需要选择分析问题。

系统自动生成至少：

```text
4 KPI
+
5 个以上 AnalysisView
+
3 条以上 Key Finding
```

必须至少覆盖：

```text
趋势
维度对比
排名
结构
异常 / 关系
```

---

# 52. Dashboard 验收标准

必须支持：

### 1

同时显示多个维度分析。

不是：

```text
用户选一个方向
↓
只显示一张图
```

---

### 2

Dashboard 至少包含：

```text
KPI
趋势
结构
对比
诊断
```

---

### 3

点击一个维度：

```text
Region = West
```

其他图同步过滤。

---

### 4

AI 洞察可以引用多个 AnalysisView。

---

### 5

Finding 可以触发下钻。

---

### 6

用户仍可以继续问自然语言问题。

---

# 53. 洞察质量验收

禁止：

```text
A 比 B 高。
C 排名第一。
D 出现下降。
```

至少应该包含：

```text
发生了什么
+
发生在哪里
+
影响多大
+
值得继续关注什么
```

更好的 Finding：

```text
销售额增长 12.8%，但利润下降 4.2%。

利润下降主要集中在 Central / Furniture，
其中 Tables 的利润贡献最低。

该组合同时存在较高折扣水平，
建议进一步检查折扣与利润的关系。
```

---

# 54. 不要做的事情

不要：

1. 继续优化旧 QuestionsPanel；
2. 给旧 Stepper 换皮；
3. 只增加更多推荐问题；
4. 只增加更多图表类型；
5. 让 LLM 自由计算数字；
6. 让用户手动配置大量分析参数；
7. 把十几张图全部塞进总览页；
8. 让每个 Dashboard 都使用完全相同的固定图表；
9. 删除现有确定性计算与验证机制；
10. 为了快速上线而继续沿用“单 Plan”作为最高层对象。

---

# 55. 本次重构真正的架构变化

旧：

```text
Question
↓
Plan
↓
Result
↓
Chart
↓
Insight
```

新：

```text
Dataset
↓
Data Understanding
↓
Analysis Bundle
↓
多个 Analysis View
↓
Batch Execution
↓
Dashboard Synthesis
↓
Cross-view Findings
↓
Interactive Dashboard
↓
AI Follow-up / Drilldown
```

---

# 56. 最终用户体验

用户：

```text
上传 Superstore.csv
```

系统：

```text
识别数据
↓
自动完成多维分析
↓
生成 Dashboard
```

用户第一眼看到：

```text
销售额
利润
利润率
订单

销售趋势
利润趋势
区域表现
品类表现
盈利诊断
异常分析

AI：
销售增长但利润下降，
问题主要集中在
Central / Furniture / Tables。
```

用户点击：

```text
Central
```

所有图表同步变化。

用户继续问：

```text
为什么 Central 的利润低？
```

AI：

```text
自动生成新的分析计划
↓
执行
↓
新增分析结果
↓
给出基于真实计算结果的回答
```

这才是本项目需要达到的：

> AI 数据分析工作台。

---

# 57. Trae 执行要求

请按以下要求修改代码：

1. 先理解现有架构，不要直接大规模删除代码。
2. 优先复用现有 profiler、quality、engine、executor、validator。
3. 新增 Bundle 层，不要强行把多个任务塞进现有 AnalysisPlan。
4. 前后端数据契约必须先定义，再写 UI。
5. 每完成一个 Phase 都要保证项目可运行。
6. 所有新增 API 都补类型。
7. 前端禁止使用大量 hardcode mock 数据冒充真实功能完成。
8. 新 Dashboard 必须使用真实执行结果。
9. 保留结果可追溯能力，但从主界面隐藏技术细节。
10. 完成每个 Phase 后运行测试并修复 lint / type error。
11. 不允许只修改文案和 UI 样式来“假装完成重构”。
12. 核心判断标准是：一个数据集能否一次生成多个分析结果，并组成一个真正可交互的 Dashboard。

---

# 58. 最优先完成的 MVP

如果工作量过大，请先完成：

```text
上传
↓
数据理解
↓
AnalysisBundle
↓
自动生成 6 个分析 View
↓
批量执行
↓
Dashboard
↓
KPI + 4 张图 + 3 条跨图洞察
```

第二阶段再做：

```text
图表点击联动
全局筛选
自动下钻
AI动态新增 View
```

不要为了完整性一次做太多而导致基础链路跑不通。

---

# 最终目标

本次改造完成后，项目必须从：

> “AI 推荐几个问题，让用户选择一个，然后生成一张图”

升级为：

> “AI 主动理解整份数据，自动生成多个分析视角，找出值得关注的变化、差异和异常，并组织成可继续探索的交互式 Dashboard。”

最终产品形态应更接近：

```text
Tableau / Power BI 的多视图 Dashboard
+
AI 自动分析
+
自然语言追问
+
确定性计算与证据链
```

而不是聊天机器人外加一张图表。
