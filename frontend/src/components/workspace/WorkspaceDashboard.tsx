"use client";

// Workspace · Dashboard 视图：12-column 布局渲染 +
// 全局筛选 / 联动高亮 / 折叠证据 / 自然语言追问（产物追加）。

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type {
  CollapseGroup,
  CollapseMember,
  DashboardArtifact,
  DashboardLayoutItem,
  FilterDefinition,
} from "@/lib/types";
import { ViewChartCard } from "@/components/dashboard/DashboardChart";
import { chartGridClass, collapseMemberIds, overviewKpiIndex, visibleDashboardThemes } from "@/lib/dashboard-layout";
import { Button, ErrorBanner } from "@/components/ui";

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
  const [findingsBusy, setFindingsBusy] = useState(false);
  const [askBusy, setAskBusy] = useState(false);
  const [question, setQuestion] = useState("");
  const [revealed, setRevealed] = useState<Set<string>>(new Set());
  const [showEvidence, setShowEvidence] = useState(false);
  const [locatedId, setLocatedId] = useState<string | null>(null);
  const focusId = highlightedId ?? locatedId;
  const [activeTab, setActiveTab] = useState<string>("重点");
  const itemRefs = useRef<Record<string, HTMLDivElement | null>>({});

  // 运行版本切换时回到「重点」并清空展开状态
  useEffect(() => {
    setActiveTab("重点");
    setRevealed(new Set());
    setShowEvidence(false);
    setLocatedId(null);
  }, [dashboard.run_id]);

  // Group members share the same DOM target; select the evidence before scrolling.
  useEffect(() => {
    if (!focusId) return;
    setActiveTab("重点");
    setRevealed((current) => new Set(current).add(focusId));
    const frame = requestAnimationFrame(() => {
      itemRefs.current[focusId]?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    const timer = setTimeout(() => {
      setLocatedId(null);
      if (highlightedId) onConsumeHighlight();
    }, 2400);
    return () => {
      cancelAnimationFrame(frame);
      clearTimeout(timer);
    };
  }, [focusId, highlightedId, onConsumeHighlight]);

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

  const refreshFindings = async () => {
    setFindingsBusy(true);
    setError(null);
    try { onReloaded(await api.refreshFindings(sessionId)); }
    catch (error) { setError(error instanceof ApiError ? error.message : "更新关键发现失败。"); }
    finally { setFindingsBusy(false); }
  };

  const themes = visibleDashboardThemes(dashboard);
  const currentTab = activeTab === "重点" || themes.some((theme) => theme.name === activeTab)
    ? activeTab : "重点";
  const layoutItems = dashboard.layout?.items ?? [];
  const collapseGroups = dashboard.collapse_groups ?? [];
  const collapsedIds = new Set(
    collapseGroups.flatMap((group) => group.members.flatMap(collapseMemberIds)),
  );
  const activeTheme = themes.find((theme) => theme.name === currentTab);
  const inTheme = (viewId: string) => !activeTheme || activeTheme.view_ids.includes(viewId);
  const standaloneItems = layoutItems.filter((item) =>
    item.role !== "kpi" && item.role !== "findings" &&
    !collapsedIds.has(item.item_id) && overviewKpiIndex(dashboard, item.item_id) < 0 &&
    dashboard.views[item.item_id]?.consumable,
  );
  const contentItems = standaloneItems.filter((item) => !item.default_hidden && inTheme(item.item_id));
  const hiddenItems = standaloneItems.filter((item) => item.default_hidden && inTheme(item.item_id));
  const visibleGroups = collapseGroups.filter((group) =>
    group.members.some((member) => collapseMemberIds(member).some(inTheme)),
  );
  const ringFor = (viewId: string) => focusId === viewId ? "ring-2 ring-apple ring-offset-2" : "";

  const renderItem = (item: DashboardLayoutItem) => {
    const card = dashboard.views[item.item_id];
    const collapsed = item.default_hidden && !revealed.has(item.item_id);
    return (
      <div
        key={item.item_id}
        ref={(element) => { itemRefs.current[item.item_id] = element; }}
        className={`${collapsed ? "col-span-12 lg:col-span-6" : chartGridClass(card)} min-w-0 scroll-mt-20 rounded-3xl border border-hairline bg-white p-4 shadow-card ${ringFor(item.item_id)}`}
      >
        {collapsed ? (
          <button
            type="button"
            onClick={() => setRevealed((current) => new Set(current).add(item.item_id))}
            aria-expanded={false}
            className="flex w-full items-center justify-between gap-3 text-left"
          >
            <span className="text-sm font-medium text-muted">{card.title}</span>
            <span className="shrink-0 text-xs font-medium text-apple">展开</span>
          </button>
        ) : (
          <>
            <div className="mb-3 flex items-start justify-between gap-3">
              <h3 className="text-sm font-semibold tracking-tight text-ink">{card.title}</h3>
              {item.default_hidden && (
                <button
                  type="button"
                  onClick={() => setRevealed((current) => {
                    const next = new Set(current);
                    next.delete(item.item_id);
                    return next;
                  })}
                  aria-expanded={true}
                  className="shrink-0 text-xs text-faint hover:text-muted"
                >收起</button>
              )}
            </div>
            {card.interpretation && (
              <p className="mb-3 border-l-2 border-apple/40 pl-2.5 text-xs leading-relaxed text-muted">
                {card.interpretation}
              </p>
            )}
            <ViewChartCard key={`${dashboard.run_id}:${card.view_id}`} card={card} />
          </>
        )}
      </div>
    );
  };

  return (
    <div>
      {dashboard.state !== "empty" && dashboard.kpis.length > 0 && (
        <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {dashboard.kpis.map((kpi, index) => (
            <div
              key={kpi.label}
              ref={(element) => {
                layoutItems.filter((item) => overviewKpiIndex(dashboard, item.item_id) === index)
                  .forEach((item) => { itemRefs.current[item.item_id] = element; });
              }}
              className={`min-w-0 scroll-mt-20 rounded-3xl border border-hairline bg-white p-5 shadow-card ${
                focusId && overviewKpiIndex(dashboard, focusId) === index ? "ring-2 ring-apple ring-offset-2" : ""
              }`}
            >
              <KpiPanel index={index} dashboard={dashboard} />
            </div>
          ))}
        </div>
      )}

      {dashboard.global_filters.length > 0 && (
        <div className="rounded-3xl border border-hairline bg-white p-5 shadow-card">
          <p className="text-xs font-medium text-muted">全局筛选</p>
          <div className="mt-3 space-y-3">
            {dashboard.global_filters.map((filter) => {
              const selected = selectedOf(filter);
              return (
                <div key={filter.column} className="flex flex-wrap items-center gap-2">
                  <span className="mr-1 text-xs font-medium text-faint">{filter.label}</span>
                  {filter.members.map((member) => (
                    <button
                      key={member}
                      type="button"
                      aria-pressed={selected.has(member)}
                      onClick={() => void toggleMember(filter, member)}
                      className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                        selected.has(member) ? "border-apple bg-apple text-white" : "border-line bg-white text-muted hover:text-ink"
                      }`}
                    >{member}</button>
                  ))}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {error && <div className="mt-5"><ErrorBanner message={error} onRetry={() => setError(null)} /></div>}
      {askNotice && (
        <div className="mt-5 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">{askNotice}</div>
      )}

      {dashboard.state === "empty" ? (
        <div className="mt-8 rounded-3xl bg-canvas px-8 py-16 text-center">
          <p className="text-base font-medium text-ink">当前筛选条件下没有数据</p>
          <p className="mt-2 text-sm text-muted">请放宽筛选条件后重试。</p>
        </div>
      ) : (
        <>
          {themes.length > 0 && (
            <div className="mt-6 flex flex-wrap items-center gap-1.5 rounded-2xl bg-canvas p-1">
              {["重点", ...themes.map((theme) => theme.name)].map((tab) => (
                <button
                  key={tab}
                  type="button"
                  aria-pressed={currentTab === tab}
                  onClick={() => setActiveTab(tab)}
                  className={`rounded-full px-4 py-1.5 text-xs font-medium transition-colors ${
                    currentTab === tab ? "bg-white text-ink shadow-sm" : "text-muted hover:text-ink"
                  }`}
                >{tab}</button>
              ))}
            </div>
          )}

          {currentTab === "重点" && (
            <div className="mt-6 rounded-3xl border border-hairline bg-white p-5 shadow-card">
              <FindingsPanel dashboard={dashboard} onLocate={setLocatedId} onRefresh={refreshFindings} busy={findingsBusy} />
            </div>
          )}

          <div className="mt-6 grid grid-cols-12 items-start gap-4">
            {visibleGroups.map((group) => (
              <CollapseCard
                key={`${dashboard.run_id}:${group.group_id}`}
                group={group}
                dashboard={dashboard}
                preferredViewId={focusId ?? undefined}
                onTarget={(element) => {
                  group.members.flatMap(collapseMemberIds).forEach((viewId) => { itemRefs.current[viewId] = element; });
                }}
              />
            ))}
            {contentItems.map(renderItem)}
          </div>

          {hiddenItems.length > 0 && (
            <div className="mt-6">
              <button
                type="button"
                aria-expanded={showEvidence}
                onClick={() => setShowEvidence((current) => !current)}
                className="text-xs font-medium text-muted hover:text-ink"
              >{showEvidence ? "收起更多分析" : `更多分析 · ${hiddenItems.length}`}</button>
              <div className="mt-4 grid grid-cols-12 items-start gap-4">
                {hiddenItems.filter((item) => showEvidence || revealed.has(item.item_id)).map(renderItem)}
              </div>
            </div>
          )}
        </>
      )}

      <div className="mt-8 rounded-3xl border border-hairline bg-white p-5 shadow-card">
        <p className="text-xs font-medium text-muted">继续追问</p>
        <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:items-center">
          <input
            type="text"
            aria-label="继续追问"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => { if (event.key === "Enter" && !askBusy) void submitAsk(); }}
            placeholder="例如：不同区域的利润如何对比？"
            className="min-w-0 flex-1 rounded-2xl border border-line bg-white px-4 py-2.5 text-sm text-ink outline-none focus:border-apple"
          />
          <Button size="sm" loading={askBusy} onClick={() => void submitAsk()}>提问</Button>
        </div>
      </div>
    </div>
  );
}

type SelectorKey = "scope_label" | "metric_label" | "measure_label" | "dimension_label" | "variant_label";
const selectorLabels: Record<SelectorKey, string> = {
  scope_label: "范围", metric_label: "指标", measure_label: "口径",
  dimension_label: "维度", variant_label: "排名",
};
const selectorKeys: SelectorKey[] = ["scope_label", "metric_label", "measure_label", "dimension_label", "variant_label"];
const memberValue = (member: CollapseMember, key: SelectorKey) =>
  member[key] || (key === "scope_label" ? "全部数据" : key === "measure_label" ? "金额" : "");

export function CollapseCard({
  group, dashboard, preferredViewId, onTarget,
}: {
  group: CollapseGroup;
  dashboard: DashboardArtifact;
  preferredViewId?: string;
  onTarget?: (element: HTMLDivElement | null) => void;
}) {
  const [selectedId, setSelectedId] = useState("");
  const members = group.members;
  const preferredMember = members.find((member) => collapseMemberIds(member).includes(preferredViewId ?? ""));
  const active = members.find((member) => member.view_id === selectedId) ?? preferredMember ?? members[0];
  useEffect(() => {
    if (preferredMember) setSelectedId(preferredMember.view_id);
  }, [preferredMember?.view_id]);
  const card = active ? dashboard.views[active.view_id] : undefined;
  if (!card || !active) return null;

  const optionsFor = (key: SelectorKey) => {
    const preceding = selectorKeys.slice(0, selectorKeys.indexOf(key));
    return Array.from(new Set(members.filter((member) =>
      preceding.every((field) => memberValue(member, field) === memberValue(active, field)),
    ).map((member) => memberValue(member, key)))).filter(Boolean);
  };
  const selectMember = (key: SelectorKey, value: string) => {
    const preceding = selectorKeys.slice(0, selectorKeys.indexOf(key));
    const candidates = members.filter((member) => memberValue(member, key) === value &&
      preceding.every((field) => memberValue(member, field) === memberValue(active, field)));
    const score = (member: CollapseMember) => selectorKeys.reduce((total, field, index) =>
      total + (memberValue(member, field) === memberValue(active, field) ? 2 ** (5 - index) : 0), 0);
    const next = candidates.sort((a, b) => score(b) - score(a))[0];
    if (next) setSelectedId(next.view_id);
  };
  const selectors = selectorKeys.filter((key) =>
    !(group.kind === "trend" && key === "dimension_label") && optionsFor(key).length > 1,
  );
  const interpretation = card.interpretation?.replace(/，用于[^。]*。?$/, "。");
  return (
    <div ref={onTarget} id={`evidence-group-${group.group_id}`}
      className={`${chartGridClass(card)} min-w-0 scroll-mt-20 rounded-3xl border border-hairline bg-white p-4 shadow-card ${preferredMember ? "ring-2 ring-apple ring-offset-2" : ""}`}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 className="text-sm font-semibold tracking-tight text-ink">{group.label}</h3>
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          {selectors.map((key) => (
            <label key={key} className="flex min-w-0 max-w-full items-center gap-1.5 text-xs text-muted">
              <span className="shrink-0">{selectorLabels[key]}</span>
              <select aria-label={`${group.label}${selectorLabels[key]}`} value={memberValue(active, key)}
                onChange={(event) => selectMember(key, event.target.value)}
                className="min-w-0 max-w-full rounded-lg border-0 bg-canvas py-1.5 pl-2 pr-6 text-xs font-medium text-ink focus:outline-none">
                {optionsFor(key).map((value) => <option key={value} value={value}>{value}</option>)}
              </select>
            </label>
          ))}
        </div>
      </div>
      {optionsFor("scope_label").length === 1 && active.scope_label && active.scope_label !== "全部数据" && (
        <p className="mt-2 text-xs text-faint">{active.scope_label}</p>
      )}
      {interpretation && <p className="mt-2 text-xs leading-relaxed text-muted">{interpretation}</p>}
      <div className="mt-3"><ViewChartCard key={`${dashboard.run_id}:${active.view_id}`} card={card} /></div>
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
  dashboard, onLocate, onRefresh, busy,
}: {
  dashboard: DashboardArtifact;
  onLocate: (viewId: string) => void;
  onRefresh: () => void;
  busy: boolean;
}) {
  return (
    <div>
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold tracking-tight text-ink">关键发现</h3>
        <button type="button" disabled={busy} onClick={onRefresh}
          className="text-xs text-muted hover:text-ink disabled:opacity-50">{busy ? "更新中…" : "更新发现"}</button>
      </div>
      {dashboard.findings_notice && <p className="mt-2 text-xs text-faint">{dashboard.findings_notice}</p>}
      {dashboard.findings.length === 0 && <p className="mt-3 text-xs text-muted">当前没有证据充分的关键发现。</p>}
      <ul className="mt-3 space-y-2">
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
              <span className="mt-1 line-clamp-2 block text-xs leading-relaxed text-muted" title={f.summary}>
                {f.summary}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
