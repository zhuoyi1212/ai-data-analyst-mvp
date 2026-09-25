// 用户可见的 7 个工作流节点（后端 10 个 stage 在 UI 上合并执行/校验/可视化）。

export interface WorkflowStep {
  key: string;
  label: string;
  hint: string;
  backendStages: string[];
}

export const WORKFLOW_STEPS: WorkflowStep[] = [
  { key: "upload", label: "上传数据", hint: "CSV / Excel，≤20MB", backendStages: ["upload"] },
  { key: "profile", label: "语义确认", hint: "确认字段含义", backendStages: ["profile"] },
  { key: "quality", label: "数据质量", hint: "逐项确认处理方式", backendStages: ["quality"] },
  { key: "questions", label: "选择问题", hint: "AI 推荐 + 自定义", backendStages: ["questions"] },
  { key: "plan", label: "确认方案", hint: "有限算子，可调整", backendStages: ["plan"] },
  {
    key: "run",
    label: "执行与校验",
    hint: "引擎计算，五项校验",
    backendStages: ["execute", "validate", "visualize"],
  },
  {
    key: "insight",
    label: "洞察与追问",
    hint: "结论必带证据",
    backendStages: ["insight", "followup"],
  },
];

// 后端 meta.stage → 当前 UI 步骤下标
export function stepIndexFromBackendStage(stage: string): number {
  for (let i = 0; i < WORKFLOW_STEPS.length; i++) {
    if (WORKFLOW_STEPS[i].backendStages.includes(stage)) return i;
  }
  return 0;
}

// 语义类型中文标签
export const SEMANTIC_LABELS: Record<string, string> = {
  dimension: "分类维度",
  metric: "数值指标",
  date: "日期",
  id: "唯一标识",
  geo: "地区",
  unknown: "待确认",
};

// 问题分类中文标签与配色
export const CATEGORY_LABELS: Record<string, string> = {
  overview: "总体概览",
  trend: "趋势分析",
  comparison: "对比分析",
  share: "占比分析",
  ranking: "排名分析",
  anomaly: "异常分析",
  correlation: "相关分析",
};

// 算子中文说明
export const OP_LABELS: Record<string, string> = {
  filter: "筛选",
  aggregate: "汇总聚合",
  group_by: "分组聚合",
  share: "占比拆解",
  top_n: "Top N 排名",
  time_series: "时间序列",
  period_compare: "同环比",
  compare_groups: "分组对比",
  correlation: "相关性",
  outlier_flag: "离群标记",
};

// 质量处理动作中文标签
export const ACTION_LABELS: Record<string, string> = {
  keep: "保留不处理",
  drop_rows: "删除相关行",
  fill_value: "填充指定值",
  fill_mean: "用均值填充",
  fill_median: "用中位数填充",
  fill_mode: "用众数填充",
  convert: "自动转换格式",
  keep_text: "按文本保留",
  drop_duplicates: "删除重复行",
  mark: "标记但保留",
  exclude: "排除离群行",
};

export const ISSUE_LABELS: Record<string, string> = {
  missing: "缺失值",
  format: "格式异常",
  duplicate: "重复数据",
  outlier: "离群值",
};
