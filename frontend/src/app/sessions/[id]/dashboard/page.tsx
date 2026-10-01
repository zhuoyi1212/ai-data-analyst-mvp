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
      <div className="mb-3 flex items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-zinc-800">{card.title}</h3>
          <p className="mt-0.5 text-xs text-zinc-400">{card.question}</p>
        </div>
        <Badge tone={card.validity === "pass" ? "green" : "amber"}>
          {card.validity}
        </Badge>
      </div>
      <ViewChartCard key={`${runId}:${card.view_id}`} card={card} />
    </Card>
  );
}

const STATE_META: Record<string, { label: string; tone: "green" | "amber" | "neutral" }> = {
  ready: { label: "就绪", tone: "green" },
  partial: { label: "部分视角不可用", tone: "amber" },
  empty: { label: "空态", tone: "neutral" },
};

const IMPORTANCE_TONE: Record<string, "red" | "amber" | "neutral"> = {
  high: "red",
  medium: "amber",
  low: "neutral",
};

function fmtValue(value: number, unit: string) {
  if (unit === "%") return `${(value * 100).toFixed(1)}%`;
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function KpiCard({ kpi }: { kpi: KPI }) {
  const positive = (kpi.change ?? 0) > 0;
  return (
    <Card className="py-4">
      <p className="text-xs text-zinc-400">{kpi.label}</p>
      <p className="mt-1 text-xl font-semibold text-zinc-900">
        {kpi.value === null ? (
          <span className="text-zinc-300">—</span>
        ) : (
          fmtValue(kpi.value, kpi.unit)
        )}
      </p>
      {kpi.change_status ? (
        <p className="mt-1 text-xs text-zinc-500">{kpi.change_status}</p>
      ) : kpi.change !== null ? (
        <p className={`mt-1 text-xs ${positive ? "text-emerald-600" : "text-red-600"}`}>
          {positive ? "▲" : "▼"} {(Math.abs(kpi.change) * 100).toFixed(1)}%
          {kpi.change_type ? ` · ${kpi.change_type}` : ""}
        </p>
      ) : null}
      {kpi.change_hint && (
        <p className="mt-1 text-xs text-zinc-400">{kpi.change_hint}</p>
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
      const all = artifact.global_filters.find((f) => f.column === column)?.members ?? [];
      return { ...d, [column]: cur.size === 0 ? all : Array.from(cur) };
    });
  }

  function apply() {
    const filters = artifact.global_filters
      .map((f) => ({ column: f.column, values: draft[f.column] ?? [] }))
      .filter((f) => f.values.length > 0 && f.values.length < (
        artifact.global_filters.find((x) => x.column === f.column)?.members.length ?? 0
      ));
    onApply(filters);
  }

  if (!artifact.global_filters.length) {
    return (
      <p className="text-sm text-zinc-400">当前数据没有可用的全局筛选维度。</p>
    );
  }

  return (
    <div>
      <div className="space-y-3">
        {artifact.global_filters.map((f) => (
          <div key={f.column} className="flex flex-wrap items-center gap-2 text-sm">
            <span className="w-16 shrink-0 text-zinc-500">{f.label}</span>
            {f.members.map((m) => {
              const active = (draft[f.column] ?? f.members).includes(m);
              return (
                <button
                  key={m}
                  onClick={() => toggle(f.column, m)}
                  className={`rounded-full border px-3 py-0.5 text-xs transition-colors ${
                    active
                      ? "border-accent bg-accent text-white"
                      : "border-zinc-300 bg-white text-zinc-500"
                  }`}
                >
                  {m}
                </button>
              );
            })}
          </div>
        ))}
      </div>
      <div className="mt-4 flex gap-2">
        <Button onClick={apply} loading={busy}>
          应用筛选并重算
        </Button>
        <Button variant="secondary" onClick={onClear} loading={busy}>
          清除筛选
        </Button>
      </div>
      <p className="mt-2 text-xs text-zinc-400">
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

  // 切换运行版本时折叠隐藏分析区
  useEffect(() => {
    setShowHidden(false);
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
      setError(e instanceof ApiError ? e.message : "工作台加载失败。")
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

  const clearFilters = useCallback(() => applyFilters([]), [applyFilters]);

  const openHistory = useCallback(async () => {
    setShowHistory(true);
    if (!runs.length) {
      try {
        const { runs: rs } = await api.listRuns(sessionId);
        setRuns(rs);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "历史运行加载失败。");
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
        setError(e instanceof ApiError ? e.message : "历史版本加载失败。");
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
      setError(e instanceof ApiError ? e.message : "当前版本加载失败。");
    }
  }, [sessionId]);

  if (!meta && !error) {
    return (
      <main className="flex min-h-screen items-center justify-center text-zinc-400">
        <Spinner />
      </main>
    );
  }

  const stateMeta = artifact ? STATE_META[artifact.state] : null;

  return (
    <main className="mx-auto max-w-6xl px-6 py-6">
      <header className="mb-5 flex items-center justify-between">
        <Link
          href={`/sessions/${sessionId}`}
          className="text-sm font-semibold text-zinc-900 hover:text-accent"
        >
          ← AI Data Analyst
        </Link>
        <div className="text-xs text-zinc-400">
          {meta?.filename} · 自动分析工作台
        </div>
      </header>

      <Card className="mb-5 flex flex-wrap items-center justify-between gap-3 py-4">
        <div className="flex items-center gap-3">
          <SectionTitle title="分析仪表盘" />
          {stateMeta && <Badge tone={stateMeta.tone}>{stateMeta.label}</Badge>}
          {artifact && (
            <span className="text-xs text-zinc-400">
              运行版本 {artifact.run_id.slice(0, 14)}
            </span>
          )}
        </div>
        <Button variant="secondary" onClick={openHistory}>
          历史运行
        </Button>
      </Card>

      {viewingHistory && (
        <div className="mb-4 flex items-center justify-between rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-700">
          <span>正在查看历史运行版本（只读）。</span>
          <Button variant="secondary" className="px-3 py-1 text-xs" onClick={backToCurrent}>
            返回当前版本
          </Button>
        </div>
      )}

      {stale && (
        <div className="mb-4">
          <ErrorBanner message={`${stale}\n可重新生成分析，或查看历史运行。`} />
          <div className="mt-2 flex gap-2">
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
        <div className="mb-4">
          <ErrorBanner message={error} onRetry={() => setError(null)} />
        </div>
      )}

      {showHistory && (
        <Card className="mb-5 py-4">
          <SectionTitle title="历史运行" desc="选择一个已发布的运行版本查看（审计只读）。" />
          <select
            className="w-full rounded-md border border-zinc-300 px-3 py-2 text-sm"
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
        <Card className="py-10 text-center">
          <h2 className="text-lg font-semibold text-zinc-900">自动分析工作台</h2>
          <p className="mx-auto mt-2 max-w-md text-sm text-zinc-500">
            系统将先聚焦核心 KPI、对齐趋势与一个基准拆分；弱信号分析默认隐藏，
            可按需展开。数值全部由确定性 pandas 引擎计算，LLM 不参与计算。
          </p>
          <div className="mt-6 flex justify-center">
            <Button onClick={runFullAnalysis} loading={!!busy} className="px-8">
              {busy || "开始自动分析"}
            </Button>
          </div>
          <p className="mt-4 text-xs text-zinc-400">
            前置条件：已完成语义确认与数据质量处理。
          </p>
        </Card>
      )}

      {artifact && (
        <>
          <Card className="mb-5 py-4">
            <p className="mb-3 text-xs text-zinc-400">
              分析范围：快照 {artifact.scope.snapshot_rows.toLocaleString()} 行 →
              当前参与 {artifact.scope.participating_rows.toLocaleString()} 行
            </p>
            {!viewingHistory ? (
              <FilterBar
                artifact={artifact}
                onApply={applyFilters}
                onClear={clearFilters}
                busy={!!busy}
              />
            ) : (
              <p className="text-sm text-zinc-400">历史版本不支持重新筛选。</p>
            )}
          </Card>

          {artifact.failed_views.length > 0 && (
            <div className="mb-5 space-y-2">
              {artifact.failed_views.map((f) => (
                <div
                  key={f.view_id}
                  className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-700"
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
                <div className="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                  {artifact.kpis.map((k) => (
                    <KpiCard key={k.label} kpi={k} />
                  ))}
                </div>
              )}

              {artifact.sections
                .filter((s) => s.view_ids.length > 0)
                .map((section) => (
                  <section key={section.section_id} className="mb-6">
                    <h2 className="mb-3 text-base font-semibold text-zinc-900">
                      {section.title}
                    </h2>
                    <div className="grid gap-4 lg:grid-cols-2">
                      {section.view_ids.map((vid) => {
                        const card = artifact.views[vid];
                        if (!card) return null;
                        return (
                          <ChartCardBlock
                            key={vid}
                            card={card}
                            runId={artifact.run_id}
                          />
                        );
                      })}
                    </div>
                  </section>
                ))}
            </>
          )}

          {Object.values(artifact.views).some((c) => c.default_hidden) && (
            <section className="mb-6">
              <button
                onClick={() => setShowHidden((v) => !v)}
                className="text-sm text-zinc-500 underline-offset-4 hover:text-accent hover:underline"
              >
                {showHidden
                  ? "收起隐藏分析"
                  : `显示隐藏分析（${
                      Object.values(artifact.views).filter(
                        (c) => c.default_hidden
                      ).length
                    }）`}
              </button>
              {showHidden && (
                <div className="mt-3">
                  <p className="mb-3 text-xs text-zinc-400">
                    内部证据计算：弱相关、无异常或重复信息默认不占主视图，
                    结果已保留，可在此核查。
                  </p>
                  <div className="grid gap-4 lg:grid-cols-2">
                    {Object.values(artifact.views)
                      .filter((c) => c.default_hidden)
                      .map((card) => (
                        <div key={card.view_id}>
                          {card.hide_reasons.length > 0 && (
                            <p className="mb-2 text-xs text-zinc-400">
                              隐藏原因：{card.hide_reasons.join("；")}
                            </p>
                          )}
                          <ChartCardBlock card={card} runId={artifact.run_id} />
                        </div>
                      ))}
                  </div>
                </div>
              )}
            </section>
          )}

          <section className="mb-6">
            <h2 className="mb-3 text-base font-semibold text-zinc-900">关键发现</h2>
            <div className="space-y-3">
              {artifact.findings.length === 0 ? (
                <p className="text-sm text-zinc-400">当前范围内未检测到显著信号。</p>
              ) : (
                artifact.findings.map((f) => (
                  <Card key={f.finding_id}>
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone={IMPORTANCE_TONE[f.importance] ?? "neutral"}>
                        {f.importance}
                      </Badge>
                      <h3 className="text-sm font-semibold text-zinc-800">
                        {f.title}
                      </h3>
                      <span className="text-xs text-zinc-400">{f.type}</span>
                    </div>
                    <p className="mt-2 whitespace-pre-wrap text-sm text-zinc-600">
                      {f.summary}
                    </p>
                  </Card>
                ))
              )}
            </div>
          </section>
        </>
      )}

      <footer className="mt-8 text-center text-xs text-zinc-400">
        所有数值均来自确定性计算引擎，LLM 不参与计算
      </footer>
    </main>
  );
}
