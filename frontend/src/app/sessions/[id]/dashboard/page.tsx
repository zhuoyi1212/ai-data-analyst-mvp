"use client";

// 自动分析工作台（P1 T06）。
// 上传 →（语义确认 / 质量处理）→ 自动多维分析 → Dashboard：
// KPI、全局筛选重算、图表、Findings、历史运行、部分失败、陈旧结果与空态。
// 浏览器只靠 API 绘图，不接触服务器 parquet；所有数值来自确定性引擎。

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import type {
  DashboardArtifact,
  KPI,
  RunSummary,
  SessionMeta,
  ViewCard,
} from "@/lib/types";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorBanner,
  SectionTitle,
  Spinner,
} from "@/components/ui";
import { ViewChartCard } from "@/components/dashboard/DashboardChart";

function ChartCardBlock({ card, runId }: { card: ViewCard; runId: string }) {
  return (
    <Card>
      <div className="mb-4 flex items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-ink">{card.title}</h3>
          <p className="mt-1 text-xs text-faint">{card.question}</p>
        </div>
        <Badge tone={card.validity === "pass" ? "green" : "amber"}>
          {card.validity}
        </Badge>
      </div>
      <ViewChartCard key={`${runId}:${card.view_id}`} card={card} />
    </Card>
  );
}

const STATE_META: Record<
  string,
  { label: string; tone: "green" | "amber" | "neutral" }
> = {
  ready: { label: "就绪", tone: "green" },
  partial: { label: "部分视角不可用", tone: "amber" },
  empty: { label: "空态", tone: "neutral" },
};

const IMPORTANCE_TONE: Record<string, "red" | "amber" | "neutral"> = {
  high: "red",
  medium: "amber",
  low: "neutral",
};

const IMPORTANCE_LABEL: Record<string, string> = {
  high: "高",
  medium: "中",
  low: "低",
};

// Finding 类型中文标签（后端新增类型时在此补；未命中回退原值）
const FINDING_TYPE_LABEL: Record<string, string> = {
  structure: "结构",
  risk: "风险",
  relationship: "关联",
  growth: "增长",
  decline: "下滑",
  anomaly: "异常",
  trend: "趋势",
  comparison: "对比",
  opportunity: "机会",
};

function fmtValue(value: number, unit: string) {
  if (unit === "%") return `${(value * 100).toFixed(1)}%`;
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function KpiCard({ kpi }: { kpi: KPI }) {
  const positive = (kpi.change ?? 0) > 0;
  return (
    <Card className="!p-5">
      <p className="text-xs text-faint">{kpi.label}</p>
      <p className="mt-2 text-[26px] font-semibold leading-none tracking-tight text-ink tabular-nums">
        {kpi.value === null ? (
          <span className="text-line">—</span>
        ) : (
          fmtValue(kpi.value, kpi.unit)
        )}
      </p>
      {kpi.change_status ? (
        <p className="mt-2.5 text-xs text-muted">{kpi.change_status}</p>
      ) : kpi.change !== null ? (
        <p
          className={`mt-2.5 text-xs font-medium tabular-nums ${
            positive ? "text-success" : "text-danger"
          }`}
        >
          {positive ? "▲" : "▼"} {(Math.abs(kpi.change) * 100).toFixed(1)}%
          {kpi.change_type ? ` · ${kpi.change_type}` : ""}
        </p>
      ) : null}
      {kpi.change_hint && (
        <p className="mt-1.5 text-xs text-faint">{kpi.change_hint}</p>
      )}
    </Card>
  );
}

function FilterBar({
  artifact,
  onApply,
  onClear,
  busy,
}: {
  artifact: DashboardArtifact;
  onApply: (filters: { column: string; values: string[] }[]) => void;
  onClear: () => void;
  busy: boolean;
}) {
  const [draft, setDraft] = useState<Record<string, string[]>>({});

  useEffect(() => {
    setDraft(
      Object.fromEntries(
        artifact.global_filters.map((f) => [
          f.column,
          f.selected_values ?? f.members,
        ]),
      ),
    );
  }, [artifact.run_id, artifact.global_filters]);

  function toggle(column: string, member: string) {
    setDraft((d) => {
      const cur = new Set(d[column] ?? []);
      if (cur.has(member)) cur.delete(member);
      else cur.add(member);
      const all =
        artifact.global_filters.find((f) => f.column === column)?.members ??
        [];
      return { ...d, [column]: cur.size === 0 ? all : Array.from(cur) };
    });
  }

  function apply() {
    const filters = artifact.global_filters
      .map((f) => ({ column: f.column, values: draft[f.column] ?? [] }))
      .filter(
        (f) =>
          f.values.length > 0 &&
          f.values.length <
            (artifact.global_filters.find((x) => x.column === f.column)
              ?.members.length ?? 0),
      );
    onApply(filters);
  }

  if (!artifact.global_filters.length) {
    return (
      <p className="text-sm text-faint">
        当前数据没有可用的全局筛选维度。
      </p>
    );
  }

  return (
    <div>
      <div className="space-y-3.5">
        {artifact.global_filters.map((f) => (
          <div
            key={f.column}
            className="flex flex-wrap items-center gap-2 text-sm"
          >
            <span className="w-14 shrink-0 text-xs font-medium text-muted">
              {f.label}
            </span>
            {f.members.map((m) => {
              const active = (draft[f.column] ?? f.members).includes(m);
              return (
                <button
                  key={m}
                  onClick={() => toggle(f.column, m)}
                  className={`rounded-full border px-3.5 py-1 text-xs transition-all active:scale-95 ${
                    active
                      ? "border-apple bg-apple-soft font-medium text-apple"
                      : "border-line bg-white text-muted hover:border-faint"
                  }`}
                >
                  {m}
                </button>
              );
            })}
          </div>
        ))}
      </div>
      <div className="mt-5 flex gap-2.5">
        <Button onClick={apply} loading={busy}>
          应用筛选并重算
        </Button>
        <Button variant="secondary" onClick={onClear} loading={busy}>
          清除筛选
        </Button>
      </div>
      <p className="mt-3 text-xs text-faint">
        筛选会重新执行全部视角、KPI 与 Findings，不是在已有图表上简单隐藏。
      </p>
    </div>
  );
}

export default function WorkbenchPage() {
  const params = useParams<{ id: string }>();
  const sessionId = params.id;

  const [meta, setMeta] = useState<SessionMeta | null>(null);
  const [artifact, setArtifact] = useState<DashboardArtifact | null>(null);
  const [stale, setStale] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const [viewingHistory, setViewingHistory] = useState(false);
  const [showHidden, setShowHidden] = useState(false);
  const [highlightedView, setHighlightedView] = useState<string | null>(null);

  // 切换运行版本时折叠隐藏分析区、清除证据高亮
  useEffect(() => {
    setShowHidden(false);
    setHighlightedView(null);
  }, [artifact?.run_id]);

  const loadInitial = useCallback(async () => {
    const { meta: m } = await api.getSession(sessionId);
    setMeta(m);
    try {
      const a = await api.getDashboard(sessionId);
      setArtifact(a);
      setStale(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setStale(e.message);
      } else if (e instanceof ApiError && e.status === 404) {
        setArtifact(null);
      } else {
        throw e;
      }
    }
  }, [sessionId]);

  useEffect(() => {
    loadInitial().catch((e) =>
      setError(e instanceof ApiError ? e.message : "工作台加载失败。"),
    );
  }, [loadInitial]);

  const runFullAnalysis = useCallback(async () => {
    setError(null);
    setStale(null);
    setViewingHistory(false);
    try {
      setBusy("正在生成分析蓝图…");
      await api.postBundle(sessionId);
      setBusy("正在批量执行分析视角…");
      await api.executeBundle(sessionId);
      setBusy("正在合成仪表盘…");
      const a = await api.postDashboard(sessionId);
      setArtifact(a);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "自动分析失败。");
    } finally {
      setBusy("");
    }
  }, [sessionId]);

  const applyFilters = useCallback(
    async (filters: { column: string; values: string[] }[]) => {
      setError(null);
      try {
        setBusy("正在按筛选条件重算…");
        const a = await api.refineDashboard(sessionId, filters);
        setArtifact(a);
        setViewingHistory(false);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "筛选重算失败。");
      } finally {
        setBusy("");
      }
    },
    [sessionId],
  );

  const clearFilters = useCallback(
    () => applyFilters([]),
    [applyFilters],
  );

  // 从 Finding 跳到证据视图：自动展开隐藏分析区 → 滚动定位 → 短暂高亮
  const jumpToEvidence = useCallback((viewId: string) => {
    setShowHidden(true);
    window.setTimeout(() => {
      const el = document.getElementById(`evidence-${viewId}`);
      if (!el) return;
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      setHighlightedView(viewId);
      window.setTimeout(
        () => setHighlightedView((cur) => (cur === viewId ? null : cur)),
        2000,
      );
    }, 60);
  }, []);

  const openHistory = useCallback(async () => {
    setShowHistory(true);
    if (!runs.length) {
      try {
        const { runs: rs } = await api.listRuns(sessionId);
        setRuns(rs);
      } catch (e) {
        setError(
          e instanceof ApiError ? e.message : "历史运行加载失败。",
        );
      }
    }
  }, [runs.length, sessionId]);

  const selectHistoryRun = useCallback(
    async (runId: string) => {
      if (!runId) return;
      setError(null);
      try {
        const a = await api.getRunDashboard(sessionId, runId);
        setArtifact(a);
        setViewingHistory(true);
      } catch (e) {
        setError(
          e instanceof ApiError ? e.message : "历史版本加载失败。",
        );
      }
    },
    [sessionId],
  );

  const backToCurrent = useCallback(async () => {
    setError(null);
    try {
      const a = await api.getDashboard(sessionId);
      setArtifact(a);
      setViewingHistory(false);
    } catch (e) {
      setError(
        e instanceof ApiError ? e.message : "当前版本加载失败。",
      );
    }
  }, [sessionId]);

  if (!meta && !error) {
    return (
      <main className="flex min-h-screen items-center justify-center text-faint">
        <Spinner className="h-6 w-6 text-apple" />
      </main>
    );
  }

  const stateMeta = artifact ? STATE_META[artifact.state] : null;

  return (
    <div className="min-h-screen">
      <div className="glass-bar fixed inset-x-0 top-0 z-50 border-b border-hairline">
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-6">
          <Link
            href={`/sessions/${sessionId}`}
            className="text-sm font-semibold text-ink transition-colors hover:text-apple"
          >
            ← AI Data Analyst
          </Link>
          <div className="truncate text-xs text-faint">
            {meta?.filename} · 自动分析工作台
          </div>
        </div>
      </div>

      <main className="mx-auto max-w-6xl px-6 pb-10 pt-24">
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-semibold tracking-tight text-ink">
              分析仪表盘
            </h1>
            {stateMeta && (
              <Badge tone={stateMeta.tone}>{stateMeta.label}</Badge>
            )}
            {artifact && (
              <span className="font-mono text-xs text-faint">
                {artifact.run_id.slice(0, 14)}
              </span>
            )}
          </div>
          <Button variant="secondary" onClick={openHistory}>
            历史运行
          </Button>
        </div>

        {viewingHistory && (
          <div className="mb-5 flex items-center justify-between rounded-2xl border border-warning/20 bg-warning-soft px-5 py-3 text-sm text-warning">
            <span>正在查看历史运行版本（只读）。</span>
            <Button
              variant="secondary"
              size="sm"
              onClick={backToCurrent}
            >
              返回当前版本
            </Button>
          </div>
        )}

        {stale && (
          <div className="mb-5">
            <ErrorBanner message={`${stale}\n可重新生成分析，或查看历史运行。`} />
            <div className="mt-3 flex gap-2.5">
              <Button onClick={runFullAnalysis} loading={!!busy}>
                {busy || "重新生成自动分析"}
              </Button>
              <Button variant="secondary" onClick={openHistory}>
                查看历史版本
              </Button>
            </div>
          </div>
        )}

        {error && (
          <div className="mb-5">
            <ErrorBanner message={error} onRetry={() => setError(null)} />
          </div>
        )}

        {showHistory && (
          <Card className="mb-6">
            <SectionTitle
              title="历史运行"
              desc="选择一个已发布的运行版本查看（审计只读）。"
            />
            <select
              className="field-input"
              defaultValue=""
              onChange={(e) => selectHistoryRun(e.target.value)}
            >
              <option value="" disabled>
                请选择运行版本…
              </option>
              {runs
                .filter((r) => r.has_dashboard)
                .map((r) => (
                  <option key={r.run_id} value={r.run_id}>
                    {r.run_id} · {r.status} ·{" "}
                    {r.scope.length
                      ? `筛选 ${r.scope.map((s) => s.column).join("/")}`
                      : "全量"}
                  </option>
                ))}
            </select>
          </Card>
        )}

        {!artifact && !stale && (
          <Card className="text-center">
            <h2 className="text-3xl font-semibold tracking-tight text-ink">
              自动分析工作台
            </h2>
            <p className="mx-auto mt-4 max-w-lg text-sm leading-relaxed text-muted">
              系统将先聚焦核心 KPI、对齐趋势与一个基准拆分；弱信号分析默认隐藏，
              可按需展开。数值全部由确定性 pandas 引擎计算，LLM 不参与计算。
            </p>
            <div className="mt-8 flex justify-center">
              <Button
                size="lg"
                onClick={runFullAnalysis}
                loading={!!busy}
                className="min-w-48"
              >
                {busy || "开始自动分析"}
              </Button>
            </div>
            <p className="mt-5 text-xs text-faint">
              前置条件：已完成语义确认与数据质量处理。
            </p>
          </Card>
        )}

        {artifact && (
          <>
            <Card className="mb-6 bg-canvas">
              <p className="mb-4 text-xs text-muted">
                分析范围：快照{" "}
                {artifact.scope.snapshot_rows.toLocaleString()} 行 →
                当前参与{" "}
                {artifact.scope.participating_rows.toLocaleString()} 行
              </p>
              {!viewingHistory ? (
                <FilterBar
                  artifact={artifact}
                  onApply={applyFilters}
                  onClear={clearFilters}
                  busy={!!busy}
                />
              ) : (
                <p className="text-sm text-faint">
                  历史版本不支持重新筛选。
                </p>
              )}
            </Card>

            {artifact.failed_views.length > 0 && (
              <div className="mb-6 space-y-2.5">
                {artifact.failed_views.map((f) => (
                  <div
                    key={f.view_id}
                    className="rounded-2xl border border-warning/20 bg-warning-soft px-5 py-3 text-sm text-warning"
                  >
                    视角不可用：{f.title}（{f.view_id}）— {f.reason}
                  </div>
                ))}
              </div>
            )}

            {artifact.state === "empty" ? (
              <EmptyState
                title="当前筛选范围内没有有效数据"
                desc="这不是错误；系统不会用 0 填充任何指标。请调整或清除筛选后重试。"
              />
            ) : (
              <>
                {artifact.kpis.length > 0 && (
                  <div className="mb-7 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                    {artifact.kpis.map((k) => (
                      <KpiCard key={k.label} kpi={k} />
                    ))}
                  </div>
                )}

                {artifact.sections
                  .filter((s) => s.view_ids.length > 0)
                  .map((section) => (
                    <section key={section.section_id} className="mb-7">
                      <h2 className="mb-4 text-lg font-semibold tracking-tight text-ink">
                        {section.title}
                      </h2>
                      <div className="grid gap-5 lg:grid-cols-2">
                        {section.view_ids.map((vid) => {
                          const card = artifact.views[vid];
                          if (!card) return null;
                          return (
                            <div
                              key={vid}
                              id={`evidence-${vid}`}
                              className={`scroll-mt-24 rounded-4xl transition-shadow duration-500 ${
                                highlightedView === vid
                                  ? "shadow-pop ring-2 ring-apple"
                                  : ""
                              }`}
                            >
                              <ChartCardBlock
                                card={card}
                                runId={artifact.run_id}
                              />
                            </div>
                          );
                        })}
                      </div>
                    </section>
                  ))}
              </>
            )}

            {Object.values(artifact.views).some((c) => c.default_hidden) && (
              <section className="mb-7">
                <button
                  onClick={() => setShowHidden((v) => !v)}
                  className="text-sm font-medium text-muted underline-offset-4 transition-colors hover:text-apple hover:underline"
                >
                  {showHidden
                    ? "收起隐藏分析"
                    : `显示隐藏分析（${
                        Object.values(artifact.views).filter(
                          (c) => c.default_hidden,
                        ).length
                      }）`}
                </button>
                {showHidden && (
                  <div className="mt-4">
                    <p className="mb-4 text-xs text-faint">
                      内部证据计算：弱相关、无异常或重复信息默认不占主视图，
                      结果已保留，可在此核查。
                    </p>
                    <div className="grid gap-5 lg:grid-cols-2">
                      {Object.values(artifact.views)
                        .filter((c) => c.default_hidden)
                        .map((card) => (
                          <div
                            key={card.view_id}
                            id={`evidence-${card.view_id}`}
                            className={`scroll-mt-24 rounded-4xl transition-shadow duration-500 ${
                              highlightedView === card.view_id
                                ? "shadow-pop ring-2 ring-apple"
                                : ""
                            }`}
                          >
                            {card.hide_reasons.length > 0 && (
                              <p className="mb-2 text-xs text-faint">
                                隐藏原因：{card.hide_reasons.join("；")}
                              </p>
                            )}
                            <ChartCardBlock
                              card={card}
                              runId={artifact.run_id}
                            />
                          </div>
                        ))}
                    </div>
                  </div>
                )}
              </section>
            )}

            <section className="mb-8">
              <h2 className="mb-4 text-lg font-semibold tracking-tight text-ink">
                关键发现
              </h2>
              <div className="space-y-4">
                {artifact.findings.length === 0 ? (
                  <p className="text-sm text-faint">
                    当前范围内未检测到显著信号。
                  </p>
                ) : (
                  artifact.findings.map((f) => (
                    <Card key={f.finding_id}>
                      <div className="flex flex-wrap items-center gap-2.5">
                        <Badge tone={IMPORTANCE_TONE[f.importance] ?? "neutral"}>
                          {IMPORTANCE_LABEL[f.importance] ?? f.importance}
                        </Badge>
                        <h3 className="text-sm font-semibold text-ink">
                          {f.title}
                        </h3>
                        <span className="text-xs text-faint">
                          {FINDING_TYPE_LABEL[f.type] ?? f.type}
                        </span>
                      </div>
                      <p className="mt-3 whitespace-pre-wrap text-sm leading-relaxed text-muted">
                        {f.summary}
                      </p>
                      {f.evidence_view_ids.length > 0 && (
                        <div className="mt-3.5 flex flex-wrap items-center gap-x-3 gap-y-1.5">
                          <span className="text-xs text-faint">证据：</span>
                          {f.evidence_view_ids.map((vid) => {
                            const evCard = artifact.views[vid];
                            if (!evCard) return null;
                            return (
                              <button
                                key={vid}
                                onClick={() => jumpToEvidence(vid)}
                                className="text-xs font-medium text-apple underline-offset-2 hover:underline"
                              >
                                {evCard.title}
                              </button>
                            );
                          })}
                        </div>
                      )}
                    </Card>
                  ))
                )}
              </div>
            </section>
          </>
        )}

        <footer className="mt-10 text-center text-xs text-faint">
          所有数值均来自确定性计算引擎，LLM 不参与计算
        </footer>
      </main>
    </div>
  );
}
