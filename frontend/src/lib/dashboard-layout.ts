import type { ChartSpec, CollapseMember, DashboardArtifact, ViewCard } from "./types";

// Count actual categories, not numeric values or a paged detail sample.
export function isCompactChart(card: ViewCard | undefined): boolean {
  const spec = card?.chart_spec;
  const data = card?.data;
  if (!spec || !data || data.kind !== "aggregate") return false;
  if (spec.type === "pie") return true;
  if (!["bar", "share_bar", "grouped_bar"].includes(spec.type)) return false;
  const dimension = spec.x_field ?? spec.dimension;
  if (!dimension || data.rows.length === 0) return false;
  const labels = new Set(data.rows.map((row) => String(row[dimension] ?? "")));
  return labels.size <= 6 && Array.from(labels).every((label) => label.length <= 18);
}

export function chartGridClass(card: ViewCard | undefined): string {
  return isCompactChart(card) ? "col-span-12 lg:col-span-6" : "col-span-12";
}

export function dashboardChartHeight(card: ViewCard): number {
  return isCompactChart(card) ? 220 : 260;
}

export function visibleDashboardThemes(dashboard: DashboardArtifact) {
  return (dashboard.themes ?? []).filter((theme) =>
    theme.view_ids.some((viewId) => dashboard.views[viewId]?.consumable &&
      overviewKpiIndex(dashboard, viewId) < 0),
  );
}

// Period timestamps identify aggregation buckets, not transaction times.
// Format their literal date without applying browser timezone conversions.
export function formatTimePeriod(value: unknown, granularity: ChartSpec["time_granularity"]): string {
  const text = String(value ?? "");
  const date = /^(\d{4})-(\d{2})-(\d{2})(?:T|\s|$)/.exec(text);
  if (!date) return text;
  if (granularity === "year") return date[1];
  if (granularity === "quarter") return `${date[1]} Q${Math.ceil(Number(date[2]) / 3)}`;
  if (granularity === "month") return `${date[1]}-${date[2]}`;
  return `${date[1]}-${date[2]}-${date[3]}`;
}

export function overviewKpiIndex(dashboard: DashboardArtifact, viewId: string): number {
  const card = dashboard.views[viewId];
  if (card?.type !== "overview" || !card.consumable) return -1;
  const label = card.data?.columns.find((column) => column.name === "value")?.label;
  return dashboard.kpis.findIndex((kpi) =>
    kpi.label === label || kpi.label === card.metric_label,
  );
}

export function collapseMemberIds(member: CollapseMember): string[] {
  return member.view_ids?.length ? member.view_ids : [member.view_id];
}
