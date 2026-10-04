// 后端契约的前端镜像（只保留 UI 使用到的字段；所有数值结果均来自后端引擎）。

export interface SessionMeta {
  session_id: string;
  filename: string;
  created_at: string;
  stage: string;
  shape: { rows: number; cols: number };
  physical_types: Record<string, string>;
  original_file: string;
  source_sample?: string;
  plan_hash?: string;
}

export interface Preview {
  columns: string[];
  rows: Record<string, string | number | null>[];
}

export type Confidence = "high" | "medium" | "low";
export type SemanticType =
  | "dimension"
  | "metric"
  | "date"
  | "id"
  | "geo"
  | "unknown";

export interface FieldProfile {
  name: string;
  physical_type: string;
  semantic_type: SemanticType;
  meaning: string;
  unit: string | null;
  candidates: string[];
  confidence: Confidence;
  examples: (string | number | null)[];
  is_date: boolean;
  is_metric: boolean;
  null_rate: number;
  cardinality: number;
  stats: Record<string, unknown>;
  rule_hints: string[];
  confirmed: boolean;
  confirmed_by_user: boolean;
  ignored: boolean;
}

export interface FieldRelation {
  type: "product" | "correlation";
  columns: string[];
  note: string;
  coefficient: number | null;
}

export interface DataDictionary {
  session_id: string;
  fields: FieldProfile[];
  relations: FieldRelation[];
  complete: boolean;
}

export type IssueType = "missing" | "format" | "duplicate" | "outlier";
export type IssueSeverity = "high" | "medium" | "low";

export interface QualityIssue {
  issue_id: string;
  type: IssueType;
  column: string | null;
  subtype: string;
  title: string;
  severity: IssueSeverity;
  evidence: Record<string, unknown>;
  suggested_action: string;
  available_actions: string[];
}

export interface QualityReport {
  session_id: string;
  issues: QualityIssue[];
  decisions: Record<string, { issue_id: string; action: string; params: Record<string, unknown> }>;
  complete: boolean;
  snapshot_rows: number | null;
  snapshot_hash: string | null;
}

export type QuestionCategory =
  | "overview"
  | "trend"
  | "comparison"
  | "share"
  | "ranking"
  | "anomaly"
  | "correlation";

export interface RecommendedQuestion {
  text: string;
  category: QuestionCategory;
  rationale: string;
  fields: string[];
  target_op: string;
  confidence: Confidence;
}

export interface PlanStep {
  step_id: string;
  op: string;
  params: Record<string, unknown>;
  description: string;
  depends_on: string[];
}

export interface AnalysisPlan {
  plan_id?: string | null;
  question: string;
  data_scope: string;
  steps: PlanStep[];
  expected_shape: string;
  chart_hint: string | null;
}

export interface PlanArtifact {
  question: string;
  plan: AnalysisPlan;
  chart: string;
  locked: boolean;
  plan_hash: string | null;
  source_question_index: number | null;
}

export interface StepRecord {
  step_id: string;
  op: string;
  description: string;
  params: Record<string, unknown>;
  input_rows: number;
  output_rows: number;
  output_columns: string[];
  formula: string;
  summary: Record<string, unknown>;
}

export interface Ledger {
  session_id: string;
  question: string;
  data_scope: string;
  plan_hash: string;
  snapshot_hash: string;
  snapshot_rows: number;
  participating_rows: number;
  chart: string;
  steps: StepRecord[];
  result_columns: string[];
  result_rows_total: number;
  result_preview: Record<string, string | number | null>[];
  elapsed_ms: number;
  executed_at: string;
}

export type CheckLevel = "pass" | "warn" | "fail";

export interface CheckItem {
  code: "coverage" | "cross_check" | "null_handling" | "shape" | "plan_consistency";
  title: string;
  level: CheckLevel;
  detail: string;
  numbers: Record<string, unknown>;
}

export interface ValidationReport {
  session_id: string;
  overall: "pass" | "warn" | "fail";
  items: CheckItem[];
  acknowledged: boolean;
  validated_at: string;
}

export interface GroundedNumber {
  value: number;
  label: string;
  ref_step: string;
  ref_kind: "metric" | "row" | "summary";
  ref_keys: Record<string, string>;
}

export interface Insight {
  text: string;
  type: "key_finding" | "trend" | "comparison" | "anomaly" | "risk";
  evidence_refs: string[];
  numbers: GroundedNumber[];
  confidence: Confidence;
  confidence_reason: string;
  needs_further_validation: boolean;
  disclaimer: string | null;
}

export interface InsightArtifact {
  insights: Insight[];
  result_digest: Record<string, unknown>;
  digest_row_cap: number;
}

export interface FollowUpQuestion {
  text: string;
  rationale: string;
  fields: string[];
  target_op: string;
  plan_hint: Record<string, unknown>;
}

export interface FollowUpSet {
  questions: FollowUpQuestion[];
}

// ---------------------------------------------------------------- Dashboard（T06）

export interface ColumnSchema {
  name: string;
  dtype: string;
  label: string;
}

export interface DataPage {
  page: number;
  page_size: number;
  total_rows: number;
  total_pages: number;
}

export interface DataSample {
  total_count: number;
  display_count: number;
  sample_method: string;
  note: string;
}

export interface ViewDataEnvelope {
  kind: string;
  columns: ColumnSchema[];
  rows: Record<string, string | number | null>[];
  page: DataPage | null;
  sample: DataSample | null;
  data_ref: string;
}

export interface ChartSpec {
  type: string;
  title: string;
  x_field: string | null;
  y_fields: string[];
  dimension: string | null;
  metric: string | null;
  interactive: boolean;
}

export interface ViewValidationItem {
  code: string;
  level: string;
  detail: string;
  numbers: Record<string, unknown>;
}

export interface ViewCard {
  view_id: string;
  title: string;
  question: string;
  type: string;
  section_id: string | null;
  chart_spec: ChartSpec | null;
  data: ViewDataEnvelope | null;
  interpretation?: string;
  status: string;
  validity: string;
  consumable: boolean;
  reason: string;
  checks: ViewValidationItem[];
  metric_label: string;
  // T07：角色与默认可见性 / 价值评估
  role: 'presentation' | 'computation';
  default_hidden: boolean;
  hide_reasons: string[];
  value_scores: Record<string, number>;
  // Task 7：普通视图 / 深挖探针
  ref_type?: 'view' | 'probe';
}

export interface KPI {
  label: string;
  value: number | null;
  change: number | null;
  change_type: string | null;
  change_delta: number | null;
  change_status: string;
  change_hint: string;
  unit: string;
}

export interface DashboardSection {
  section_id: string;
  title: string;
  view_ids: string[];
}

export interface FilterDefinition {
  column: string;
  label: string;
  members: string[];
  selected_values: string[] | null;
}

export interface AppliedFilter {
  column: string;
  values: string[];
}

export interface DashboardScope {
  filters: AppliedFilter[];
  snapshot_rows: number;
  participating_rows: number;
}

export interface Finding {
  finding_id: string;
  title: string;
  summary: string;
  type: string;
  evidence_view_ids: string[];
  importance: string;
  drilldown: unknown | null;
}

export interface FailedView {
  view_id: string;
  title: string;
  reason: string;
}

export interface DashboardArtifact {
  run_id: string;
  bundle_id: string;
  title: string;
  state: string;
  scope: DashboardScope;
  kpis: KPI[];
  sections: DashboardSection[];
  views: Record<string, ViewCard>;
  findings: Finding[];
  risks: Finding[];
  failed_views: FailedView[];
  global_filters: FilterDefinition[];
  layout: DashboardLayout | null;
}

// ------------------------------------------------ Task 7：12-column 布局

export type LayoutRole =
  | 'kpi' | 'hero' | 'primary' | 'supporting' | 'diagnostic' | 'findings';

export interface DashboardLayoutItem {
  item_id: string;
  role: LayoutRole;
  ref_type: 'view' | 'probe' | 'meta';
  col_span: number;
  row_span: number;
  order: number;
  rationale: string;
  default_hidden: boolean;
}

export interface DashboardLayout {
  items: DashboardLayoutItem[];
  generated_at: string;
}

// ------------------------------------------------ Task 5：证据图谱

export interface EvidenceNode {
  node_id: string;
  type: 'view' | 'probe' | 'signal' | 'chain' | 'claim';
  label: string;
}

export interface EvidenceEdge {
  source: string;
  target: string;
  type: 'evidence' | 'root' | 'uses' | 'supports';
}

export interface EvidenceGraph {
  session_id: string;
  run_id: string;
  nodes: EvidenceNode[];
  edges: EvidenceEdge[];
  created_at: string;
}

// ------------------------------------------------ Task 1：自动分析状态

export type AutoStageName =
  | 'profile' | 'quality' | 'scan' | 'signals'
  | 'diagnostic' | 'synthesis';

export interface StageLog {
  stage: AutoStageName;
  status: 'pending' | 'running' | 'completed' | 'skipped' | 'failed';
  message: string;
  started_at: string;
  ended_at: string;
}

export interface GateQuestion {
  question_id: string;
  stage: AutoStageName;
  prompt: string;
  options: string[];
  allow_free_text: boolean;
  context: Record<string, string>;
}

export interface AutoAnalysisState {
  session_id: string;
  status: 'running' | 'waiting_input' | 'completed' | 'failed';
  current_stage: AutoStageName;
  stages: StageLog[];
  needs_input: GateQuestion[];
  answers: Record<string, string>;
  run_id: string;
  artifacts: Record<string, string>;
  error?: string;
}

// ------------------------------------------------ Task 6：深度报告

export type ReportClaimType = 'fact' | 'signal' | 'hypothesis' | 'conclusion';

export interface ReportClaim {
  claim_id: string;
  text: string;
  claim_type: ReportClaimType;
  evidence_view_ids: string[];
  limitations: string;
}

export interface ReportSection {
  section_id: string;
  title: string;
  claims: ReportClaim[];
}

export interface AnalysisReportArtifact {
  session_id: string;
  run_id: string;
  sections: ReportSection[];
  evidence_graph_created_at: string;
  generated_at: string;
}

// ------------------------------------------------ Task 8：追问

export interface AskClaim {
  text: string;
  evidence_view_ids: string[];
}

export interface AskArtifact {
  ask_id: string;
  question: string;
  answer_claims: AskClaim[];
  new_view_ids: string[];
  scope_rows: number;
  appended_report_section_ids: string[];
  dashboard_refreshed?: boolean;
  created_at: string;
}

export interface AskSet {
  session_id: string;
  run_id: string;
  asks: AskArtifact[];
}

export interface RunSummary {
  run_id: string;
  bundle_id: string;
  status: string;
  scope: AppliedFilter[];
  has_dashboard: boolean;
  created_at: string;
  published_at: string | null;
}
