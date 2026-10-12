"use client";

// Workspace · Dashboard 视图：12-column 布局渲染 +
// 全局筛选 / 联动高亮 / 折叠证据 / 自然语言追问（产物追加）。

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type {
  CollapseGroup,
  DashboardArtifact,
  FilterDefinition,
} from "@/lib/types";
import { ViewChartCard } from "@/components/dashboard/DashboardChart";
import { Button, ErrorBanner, Spinner } from "@/components/ui";

interface Props {
  sessionId: string;
  dashboard: DashboardArtifact;
  onReloaded: (d: DashboardArtifact) => void;
  highlightedId: string | null;
  onConsumeHighlight: () => void;
}

export function WorkspaceDashboard({
  sessionId, dashboard, onReloaded, highlightedId, onConsumeHighlight,
}: Props) {
  const [error, setError] = useState<string | null>(null);
  const [askNotice, setAskNotice] = useState<string | null>(null);
  const [askBusy, setAskBusy] = useState(false);
  const [question, setQuestion] = useState("");
  const [revealed, setRevealed] = useState<Set<string>>(new Set());
  const [activeTab, setActiveTab] = useState<string>("重点");
  const itemRefs = useRef<Record<string, HTMLDivElement | null>>({});

  // 运行版本切换时回到「重点」并清空展开状态
  useEffect(() => {
    setActiveTab("重点");
    setRevealed(new Set());
  }, [dashboard.run_id]);

  // 高亮联动：滚入视口 + 描边，短暂后清除
  useEffect(() => {
    if (!highlightedId) return;
    const item = dashboard.layout?.items.find(
      (i) => i.item_id === highlightedId
    );
    if (item?.default_hidden) {
      setRevealed((s) => new Set(s).add(highlightedId));
    }
    const el = itemRefs.current[highlightedId];
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
    const t = setTimeout(onConsumeHighlight, 2400);
    return () => clearTimeout(t);
  }, [highlightedId, onConsumeHighlight, dashboard.layout]);

  const selectedOf = useCallback(
    (f: FilterDefinition): Set<string> =>
      new Set(
        f.selected_values === null ? f.members : f.selected_values
      ),
    [],
  );

  const toggleMember = useCallback(
    async (f: FilterDefinition, member: string) => {
      setError(null);
      const next = new Set(selectedOf(f));
      if (next.has(member)) {
        if (next.size === 1) return; // 至少保留一个
        next.delete(member);
      } else {
        next.add(member);
      }
      // 汇总当前所有筛选器的选择
      const filters = dashboard.global_filters.map((g) => ({
        column: g.column,
        values:
          g.column === f.column
            ? Array.from(next)
            : g.selected_values === null
              ? g.members
              : g.selected_values,
      }));
      try {
        const d = await api.refineDashboard(sessionId, filters);
        onReloaded(d);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "筛选重算失败。");
      }
    },
    [dashboard.global_filters, selectedOf, sessionId, onReloaded],
  );

  const submitAsk = useCallback(async () => {
    const q = question.trim();
    if (!q) return;
    setError(null);
    setAskNotice(null);
    setAskBusy(true);
    try {
      const artifact = await api.ask(sessionId, q);
      const d = await api.getDashboard(sessionId);
      onReloaded(d);
      setQuestion("");
      if (artifact.dashboard_refreshed === false) {
        setAskNotice("新视图已保存，但 Dashboard 未能即时刷新，稍后可重试。");
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "追问失败。");
    } finally {
      setAskBusy(false);
    }
  }, [question, sessionId, onReloaded]);

  const layout = dashboard.layout;
  const themes = dashboard.themes ?? [];
  const layoutItems = layout?.items ?? [];
  const collapseGroups = dashboard.collapse_groups ?? [];
  const collapsedIds = new Set(
    collapseGroups.flatMap((g) => g.members.map((m) => m.view_id)),
  );

  // KPI 条与隐藏证据不受主题过滤影响；其余按 activeTab 过滤
  const kpiItems = layoutItems.filter((i) => i.role === "kpi");
  const hiddenItems = layoutItems.filter((i) => i.default_hidden);

  const contentItems = (() => {
    if (themes.length === 0) {
      return layoutItems.filter(
        (i) => i.role !== "kpi" && !i.default_hidden && !collapsedIds.has(i.item_id),
      );
    }
    if (activeTab === "重点") {
      const rank = (r: string) =>
        r === "findings" ? 0 : r === "hero" ? 1 : r === "primary" ? 2 : 3;
      return layoutItems
        .filter(
          (i) =>
            (i.role === "findings" || i.role === "hero" || i.role === "primary") &&
            !collapsedIds.has(i.item_id),
        )
        .sort((a, b) => rank(a.role) - rank(b.role));
    }
    const theme = themes.find((t) => t.name === activeTab);
    return theme
      ? layoutItems.filter(
          (i) => theme.view_ids.includes(i.item_id) && !collapsedIds.has(i.item_id),
        )
      : [];
  })();

  const filteredItems = [...kpiItems, ...contentItems, ...hiddenItems];
  const hiddenCount = hiddenItems.length;

  return (
    <div>
      {/* ------------------------------------------- 全局筛选 */}
      {dashboard.global_filters.length > 0 && (
        <div className="rounded-3xl border border-hairline bg-white p-5 shadow-card">
          <p className="text-xs font-medium text-muted">全局筛选</p>
          <div className="mt-3 space-y-3">
            {dashboard.global_filters.map((f) => {
              const selected = selectedOf(f);
              return (
                <div key={f.column} className="flex flex-wrap items-center gap-2">
                  <span className="mr-1 text-xs font-medium text-faint">
                    {f.label}
                  </span>
                  {f.members.map((m) => {
                    const active = selected.has(m);
                    return (
                      <button
                        key={m}
                        type="button"
                        onClick={() => void toggleMember(f, m)}
                        className={`rounded-full border px-3 py-1 text-xs font-medium transition-all
                          ${active
                            ? "border-apple bg-apple text-white"
                            : "border-line bg-white text-muted hover:text-ink"}`}
                      >
                        {m}
                      </button>
                    );
                  })}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {error && (
        <div className="mt-5">
          <ErrorBanner message={error} onRetry={() => setError(null)} />
        </div>
      )}

      {askNotice && (
        <div className="mt-5 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          {askNotice}
        </div>
      )}

      {dashboard.state === "empty" && (
        <div className="mt-8 rounded-3xl bg-canvas px-8 py-16 text-center">
          <p className="text-base font-medium text-ink">
            当前筛选条件下没有数据
          </p>
          <p className="mx-auto mt-2 max-w-md text-sm text-muted">
            系统不会用 0 或空值伪造结果，请放宽筛选条件后重试。
          </p>
        </div>
      )}

      {/* ------------------------------------------- 业务主题分段 */}
      {layout && dashboard.state !== "empty" && themes.length > 0 && (
        <div className="mt-6 flex flex-wrap items-center gap-1.5 rounded-full bg-canvas p-1">
          {["重点", ...themes.map((t) => t.name)].map((tab) => (
            <button
              key={tab}
              type="button"
              onClick={() => setActiveTab(tab)}
              className={`rounded-full px-4 py-1.5 text-xs font-medium transition-all ${
                activeTab === tab
                  ? "bg-white text-ink shadow-sm"
                  : "text-muted hover:text-ink"
              }`}
            >
              {tab}
            </button>
          ))}
        </div>
      )}

      {/* ------------------------------------------- 维度折叠 */}
      {collapseGroups.length > 0 && (
        <div className="mt-6 grid grid-cols-12 gap-4">
          {collapseGroups.map((group) => (
            <CollapseCard
              key={`${dashboard.run_id}:${group.group_id}`}
              group={group}
              dashboard={dashboard}
            />
          ))}
        </div>
      )}

      {/* ------------------------------------------- 12-column 网格 */}
      {layout && dashboard.state !== "empty" && (
        <div className="mt-6 grid grid-cols-12 gap-4">
          {filteredItems.map((item) => {
            const isKpi = item.role === "kpi";
            const isFindings = item.role === "findings";
            const collapsed =
              item.default_hidden && !revealed.has(item.item_id);

            const ring =
              highlightedId === item.item_id
                ? "ring-2 ring-apple ring-offset-2"
                : "";

            return (
              <div
                key={item.item_id}
                ref={(el) => {
                  itemRefs.current[item.item_id] = el;
                }}
                style={{
                  gridColumn: `span ${item.col_span} / span ${item.col_span}`,
                }}
                className={`rounded-3xl border border-hairline bg-white p-5 shadow-card transition-all duration-500 ${ring}`}
              >
                {isKpi ? (
                  <KpiPanel
                    index={Number(item.item_id.replace("kpi_", "")) - 1}
                    dashboard={dashboard}
                  />
                ) : isFindings ? (
                  <FindingsPanel
                    dashboard={dashboard}
                    onLocate={(vid) => {
                      const el = itemRefs.current[vid];
                      el?.scrollIntoView({
                        behavior: "smooth", block: "center",
                      });
                    }}
                  />
                ) : collapsed ? (
                  <button
                    type="button"
                    onClick={() =>
                      setRevealed((s) => new Set(s).add(item.item_id))
                    }
                    className="flex w-full items-center justify-between text-left"
                  >
                    <span>
                      <span className="text-sm font-medium text-muted">
                        {dashboard.views[item.item_id]?.title ?? item.item_id}
                      </span>
                      {dashboard.views[item.item_id]?.interpretation && (
                        <span className="mt-1 block text-xs leading-relaxed text-muted">
                          {dashboard.views[item.item_id]?.interpretation}
                        </span>
                      )}
                      <span className="mt-1 block text-xs text-faint">
                        {item.rationale}
                      </span>
                    </span>
                    <span className="text-xs font-medium text-apple">
                      展开
                    </span>
                  </button>
                ) : (
                  <div>
                    <div className="mb-3 flex items-start justify-between gap-3">
                      <h3 className="text-sm font-semibold tracking-tight text-ink">
                        {dashboard.views[item.item_id]?.title}
                      </h3>
                      {item.default_hidden && (
                        <button
                          type="button"
                          onClick={() =>
                            setRevealed((s) => {
                              const n = new Set(s);
                              n.delete(item.item_id);
                              return n;
                            })
                          }
                          className="text-xs text-faint hover:text-muted"
                        >
                          收起
                        </button>
                      )}
                    </div>
                    {dashboard.views[item.item_id]?.interpretation && (
                      <p className="mb-3 border-l-2 border-apple/40 pl-2.5 text-xs leading-relaxed text-muted">
                        {dashboard.views[item.item_id]?.interpretation}
                      </p>
                    )}
                    <ViewChartCard
                      card={dashboard.views[item.item_id]}
                      chartHeight={item.role === "hero" ? 380 : 280}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* ------------------------------------------- 追问框 */}
      <div className="mt-8 rounded-3xl border border-hairline bg-white p-5 shadow-card">
        <p className="text-xs font-medium text-muted">
          继续追问 AI（结果基于当前筛选范围，新图表追加不覆盖）
        </p>
        <div className="mt-3 flex items-center gap-3">
          <input
            type="text"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void submitAsk();
            }}
            placeholder="例如：不同 region 之间 profit 如何对比？"
            className="flex-1 rounded-2xl border border-line bg-white px-4 py-2.5 text-sm text-ink outline-none transition-colors focus:border-apple"
          />
          <Button size="sm" loading={askBusy} onClick={() => void submitAsk()}>
            提问
          </Button>
        </div>
      </div>
      {hiddenCount > 0 && (
        <p className="mt-4 text-center text-xs text-faint">
          另有 {hiddenCount} 项证据视图默认折叠，可就地展开。
        </p>
      )}
    </div>
  );
}

// ----------------------------------------------------------------- 维度折叠卡

function CollapseCard({
  group, dashboard,
}: { group: CollapseGroup; dashboard: DashboardArtifact }) {
  const [activeId, setActiveId] = useState(group.members[0]?.view_id ?? "");
  const card = dashboard.views[activeId];
  if (!card) return null;
  return (
    <div className="col-span-12 rounded-3xl border border-hairline bg-white p-5 shadow-card">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 className="text-sm font-semibold tracking-tight text-ink">
          {group.label}
        </h3>
        <div className="flex flex-wrap items-center gap-1 rounded-full bg-canvas p-1">
          {group.members.map((m) => (
            <button
              key={m.view_id}
              type="button"
              onClick={() => setActiveId(m.view_id)}
              className={`rounded-full px-3 py-1 text-xs font-medium transition-all ${
                activeId === m.view_id
                  ? "bg-white text-ink shadow-sm"
                  : "text-muted hover:text-ink"
              }`}
            >
              {m.dimension_label}
            </button>
          ))}
        </div>
      </div>
      {card.interpretation && (
        <p className="mt-2 border-l-2 border-apple/40 pl-2.5 text-xs leading-relaxed text-muted">
          {card.interpretation}
        </p>
      )}
      <div className="mt-4">
        <ViewChartCard card={card} chartHeight={300} />
      </div>
    </div>
  );
}

// ----------------------------------------------------------------- KPI 条

function fmtKpi(value: number, unit: string): string {
  if (unit === "%") return `${(value * 100).toFixed(1)}%`;
  const num = value.toLocaleString(undefined, { maximumFractionDigits: 0 });
  return unit ? `${unit}${num}` : num;
}

function KpiPanel({
  index, dashboard,
}: { index: number; dashboard: DashboardArtifact }) {
  const kpi = dashboard.kpis[index];
  if (!kpi) return null;
  const change = kpi.change;
  const up = (change ?? 0) >= 0;
  return (
    <div>
      <p className="text-xs font-medium text-muted">{kpi.label}</p>
      <p className="mt-2 text-2xl font-semibold tracking-tight text-ink">
        {kpi.value === null ? (
          <span className="text-base text-faint">不可计算</span>
        ) : (
          fmtKpi(kpi.value, kpi.unit)
        )}
      </p>
      {change !== null && (
        <p
          className={`mt-1 text-xs font-medium ${
            up ? "text-success" : "text-danger"
          }`}
        >
          {up ? "▲" : "▼"} {Math.abs(change * 100).toFixed(1)}%
        </p>
      )}
      {kpi.change_status && (
        <p className="mt-1 text-xs text-faint">{kpi.change_status}</p>
      )}
    </div>
  );
}

// ----------------------------------------------------------------- Findings

function FindingsPanel({
  dashboard, onLocate,
}: {
  dashboard: DashboardArtifact;
  onLocate: (viewId: string) => void;
}) {
  return (
    <div>
      <h3 className="text-sm font-semibold tracking-tight text-ink">
        关键发现
      </h3>
      <ul className="mt-3 space-y-3">
        {dashboard.findings.map((f) => (
          <li key={f.finding_id}>
            <button
              type="button"
              disabled={f.evidence_view_ids.length === 0}
              onClick={() => onLocate(f.evidence_view_ids[0])}
              className="w-full rounded-2xl bg-canvas px-4 py-3 text-left transition-colors hover:bg-line/50 disabled:cursor-default"
            >
              <span className="flex items-center gap-2">
                <span className="text-sm font-medium text-ink">{f.title}</span>
              </span>
              <span className="mt-1 block text-xs leading-relaxed text-muted">
                {f.summary}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
