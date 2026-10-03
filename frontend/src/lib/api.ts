// 后端 API 客户端：统一经 Next.js rewrite 到 127.0.0.1:8000。
// 所有错误均提取后端中文 detail，绝不在前端伪造任何数据结果。

import type {
  AnalysisReportArtifact,
  AskArtifact,
  AutoAnalysisState,
  DashboardArtifact,
  DataDictionary,
  EvidenceGraph,
  FollowUpSet,
  Insight,
  Ledger,
  PlanArtifact,
  Preview,
  QualityReport,
  RecommendedQuestion,
  RunSummary,
  SessionMeta,
  ValidationReport,
  ViewDataEnvelope,
} from "./types";

const BASE = "/api/backend";

export class ApiError extends Error {
  status: number;
  retryable: boolean;
  reasons?: string[];
  constructor(message: string, status: number, retryable = false, reasons?: string[]) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryable = retryable;
    this.reasons = reasons;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + path, {
      ...init,
      headers:
        init?.body && !(init.body instanceof FormData)
          ? { "Content-Type": "application/json", ...(init.headers ?? {}) }
          : init?.headers,
      cache: "no-store",
    });
  } catch {
    throw new ApiError(
      "无法连接后端服务，请确认已在 backend 目录启动服务（uv run uvicorn app.main:app --port 8000）。",
      0,
      true,
    );
  }
  if (!res.ok) {
    let detail = `请求失败（HTTP ${res.status}）`;
    let retryable = false;
    let reasons: string[] | undefined;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
      if (Array.isArray(body?.reasons)) reasons = body.reasons;
      retryable = body?.retryable === true;
    } catch {
      // 非 JSON 错误体，保留默认文案
    }
    throw new ApiError(detail, res.status, retryable, reasons);
  }
  return (await res.json()) as T;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const get = <T>(path: string) => request<T>(path, { method: "GET" });

export const api = {
  health: () => get<{ status: string; fixture_mode: string; llm_configured: boolean }>("/health"),

  listSamples: () => get<{ samples: string[] }>("/sessions/samples"),
  upload: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ meta: SessionMeta; preview: Preview }>("/sessions/upload", {
      method: "POST",
      body: form,
    });
  },
  fromSample: (filename: string) =>
    post<{ meta: SessionMeta; preview: Preview }>("/sessions/from-sample", { filename }),
  getSession: (id: string) => get<{ meta: SessionMeta }>(`/sessions/${id}`),
  getPreview: (id: string) => get<Preview>(`/sessions/${id}/preview`),

  generateProfile: (id: string) =>
    post<{ dictionary: DataDictionary }>(`/sessions/${id}/profile/generate`, {}),
  getProfile: (id: string) =>
    get<{ dictionary: DataDictionary }>(`/sessions/${id}/profile`),
  confirmFields: (id: string, fields: unknown[]) =>
    post<{ dictionary: DataDictionary; complete: boolean }>(
      `/sessions/${id}/profile/confirm`,
      { fields },
    ),

  runQuality: (id: string) => post<{ report: QualityReport }>(`/sessions/${id}/quality/run`),
  getQuality: (id: string) => get<{ report: QualityReport }>(`/sessions/${id}/quality`),
  applyQuality: (id: string, decisions: Record<string, { action: string; params?: unknown }>) =>
    post<{ report: QualityReport }>(`/sessions/${id}/quality/apply`, { decisions }),

  generateQuestions: (id: string) =>
    post<{ questions: RecommendedQuestion[] }>(`/sessions/${id}/questions/generate`, {}),
  getQuestions: (id: string) =>
    get<{ questions: RecommendedQuestion[] }>(`/sessions/${id}/questions`),

  generatePlan: (id: string, question: string, source_question_index?: number) =>
    post<PlanArtifact>(`/sessions/${id}/plan/generate`, {
      question,
      source_question_index,
    }),
  getPlan: (id: string) => get<PlanArtifact>(`/sessions/${id}/plan`),
  editPlan: (id: string, plan: unknown) =>
    post<PlanArtifact>(`/sessions/${id}/plan/edit`, { plan }),
  confirmPlan: (id: string, plan?: unknown) =>
    post<PlanArtifact>(`/sessions/${id}/plan/confirm`, plan ? { plan } : {}),

  execute: (id: string) => post<Ledger>(`/sessions/${id}/execute`),
  getLedger: (id: string) => get<Ledger>(`/sessions/${id}/ledger`),

  runValidation: (id: string) => post<ValidationReport>(`/sessions/${id}/validate`, {}),
  getValidation: (id: string) => get<ValidationReport>(`/sessions/${id}/validate`),
  acknowledge: (id: string) =>
    post<ValidationReport>(`/sessions/${id}/validate/acknowledge`, {}),

  generateInsights: (id: string) =>
    post<{ insights: Insight[] }>(`/sessions/${id}/insights/generate`, {}),
  getInsights: (id: string) =>
    get<{ insights: Insight[]; result_digest?: unknown }>(`/sessions/${id}/insights`),

  generateFollowups: (id: string) =>
    post<FollowUpSet>(`/sessions/${id}/followups/generate`, {}),
  getFollowups: (id: string) => get<FollowUpSet>(`/sessions/${id}/followups`),

  // ---- 自动分析工作台（T06） ----
  postBundle: (id: string) =>
    post<unknown>(`/sessions/${id}/analysis-bundle`, {}),
  executeBundle: (id: string) =>
    post<unknown>(`/sessions/${id}/analysis-bundle/execute`, {}),
  postDashboard: (id: string) =>
    post<DashboardArtifact>(`/sessions/${id}/dashboard`, {}),
  getDashboard: (id: string) =>
    get<DashboardArtifact>(`/sessions/${id}/dashboard`),
  refineDashboard: (
    id: string,
    filters: { column: string; values: string[] }[],
  ) =>
    post<DashboardArtifact>(`/sessions/${id}/dashboard/refine`, { filters }),
  listRuns: (id: string) =>
    get<{ runs: RunSummary[] }>(`/sessions/${id}/runs`),
  getRunDashboard: (id: string, runId: string) =>
    get<DashboardArtifact>(`/sessions/${id}/runs/${runId}/dashboard`),
  getViewRows: (dataRef: string, page: number, pageSize: number) =>
    get<ViewDataEnvelope>(
      `${dataRef}?page=${page}&page_size=${pageSize}`,
    ),

  // ---- Autonomous Analyst（Task 1–8） ----
  startAuto: (id: string, answers?: Record<string, string>) =>
    post<AutoAnalysisState>(
      `/sessions/${id}/auto-analyze`,
      answers ? { answers } : {},
    ),
  getAutoState: (id: string) =>
    get<AutoAnalysisState>(`/sessions/${id}/auto-analyze`),
  getEvidence: (id: string, node?: string) =>
    get<EvidenceGraph>(
      `/sessions/${id}/evidence${node ? `?node=${encodeURIComponent(node)}` : ""}`,
    ),
  getReport: (id: string) =>
    get<AnalysisReportArtifact>(`/sessions/${id}/report`),
  ask: (id: string, question: string) =>
    post<AskArtifact>(`/sessions/${id}/ask`, { question }),
};
