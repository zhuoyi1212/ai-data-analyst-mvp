"use client";

// 按 ChartSpec 渲染图表——字段全部来自 ViewDataEnvelope，浏览器无需接触 parquet。
// 明细类（line_outlier/anomaly）渲染可翻页数据表。

import { useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, ApiError } from "@/lib/api";
import type { ViewCard } from "@/lib/types";
import { Spinner } from "@/components/ui";

const ACCENT = "#2563eb";
const PIE_COLORS = [
  "#2563eb", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444",
  "#8b5cf6", "#ec4899", "#14b8a6", "#f97316", "#6366f1",
];

type Row = Record<string, string | number | null>;

function ChartError({ message }: { message: string }) {
  return <p className="py-6 text-center text-sm text-red-500">{message}</p>;
}

function renderChart(card: ViewCard, rows: Row[]) {
  const spec = card.chart_spec;
  if (!spec) return null;

  switch (spec.type) {
    case "bar":
    case "share_bar":
    case "grouped_bar":
      return (
        <ResponsiveContainer width="100%" height={280}>
          <BarChart data={rows} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e4e4e7" />
            <XAxis dataKey={spec.x_field ?? undefined} tick={{ fontSize: 12 }} />
            <YAxis tick={{ fontSize: 12 }} />
            <Tooltip />
            <Bar dataKey={spec.y_fields[0]} fill={ACCENT} radius={[3, 3, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      );
    case "pie": {
      const key = spec.y_fields.includes("share") ? "share" : spec.y_fields[0];
      return (
        <ResponsiveContainer width="100%" height={300}>
          <PieChart>
            <Pie
              data={rows}
              dataKey={key}
              nameKey={spec.x_field ?? ""}
              label={{ fontSize: 12 }}
            >
              {rows.map((_, i) => (
                <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
              ))}
            </Pie>
            <Tooltip />
          </PieChart>
        </ResponsiveContainer>
      );
    }
    case "line":
      return (
        <ResponsiveContainer width="100%" height={280}>
          <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e4e4e7" />
            <XAxis dataKey={spec.x_field ?? undefined} tick={{ fontSize: 12 }} />
            <YAxis tick={{ fontSize: 12 }} />
            <Tooltip />
            <Line
              type="monotone"
              dataKey={spec.y_fields[0]}
              stroke={ACCENT}
              strokeWidth={2}
              dot={false}
            />
          </LineChart>
        </ResponsiveContainer>
      );
    case "scatter":
      return (
        <ResponsiveContainer width="100%" height={300}>
          <ScatterChart margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e4e4e7" />
            <XAxis
              dataKey={spec.x_field ?? ""}
              type="number"
              tick={{ fontSize: 12 }}
            />
            <YAxis dataKey={spec.y_fields[0]} tick={{ fontSize: 12 }} />
            <Tooltip cursor={{ strokeDasharray: "3 3" }} />
            <Scatter data={rows} fill={ACCENT} />
          </ScatterChart>
        </ResponsiveContainer>
      );
    default:
      return null;
  }
}

// ----------------------------------------------------------------- 明细分页表

function PagedTable({ card }: { card: ViewCard }) {
  const envelope = card.data!;
  const [rows, setRows] = useState<Row[]>(envelope.rows);
  const [page, setPage] = useState(envelope.page?.page ?? 1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const meta = envelope.page;

  async function goto(next: number) {
    setLoading(true);
    setError("");
    try {
      const res = await api.getViewRows(envelope.data_ref, next, meta?.page_size ?? 50);
      setRows(res.rows);
      setPage(res.page?.page ?? next);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "明细加载失败。");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <div className="relative overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="border-b border-zinc-200 text-zinc-500">
              {envelope.columns.map((c) => (
                <th key={c.name} className="py-2 pr-4 font-medium">
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} className="border-b border-zinc-100">
                {envelope.columns.map((c) => (
                  <td key={c.name} className="py-1.5 pr-4 text-zinc-700">
                    {row[c.name] === null ? (
                      <span className="text-zinc-300">—</span>
                    ) : (
                      String(row[c.name])
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-white/60 text-zinc-400">
            <Spinner className="h-5 w-5" />
          </div>
        )}
      </div>
      {error && <p className="mt-2 text-xs text-red-500">{error}</p>}
      {meta && (
        <div className="mt-3 flex items-center justify-between text-xs text-zinc-500">
          <span>
            共 {meta.total_rows.toLocaleString()} 行 · 第 {page}/{meta.total_pages} 页
          </span>
          <span className="flex gap-2">
            <button
              className="rounded border border-zinc-300 px-2 py-1 disabled:opacity-40"
              disabled={page <= 1 || loading}
              onClick={() => goto(page - 1)}
            >
              上一页
            </button>
            <button
              className="rounded border border-zinc-300 px-2 py-1 disabled:opacity-40"
              disabled={page >= meta.total_pages || loading}
              onClick={() => goto(page + 1)}
            >
              下一页
            </button>
          </span>
        </div>
      )}
    </div>
  );
}

// ----------------------------------------------------------------- 卡片出口

export function ViewChartCard({ card }: { card: ViewCard }) {
  const envelope = card.data;
  const isDetail = card.type === "anomaly" || card.chart_spec?.type === "line_outlier";

  if (!envelope) {
    return <ChartError message={card.reason || "该视角无数据。"} />;
  }
  if (isDetail) return <PagedTable card={card} />;
  if (!card.chart_spec) {
    return <p className="py-4 text-sm text-zinc-400">该视角以指标卡展示。</p>;
  }
  const chart = renderChart(card, envelope.rows);
  if (!chart) return <PagedTable card={card} />;

  return (
    <div>
      {chart}
      {envelope.sample && (
        <p className="mt-2 text-xs text-zinc-400">{envelope.sample.note}</p>
      )}
    </div>
  );
}
