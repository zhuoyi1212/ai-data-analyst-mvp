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
