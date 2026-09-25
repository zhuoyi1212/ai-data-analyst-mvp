"use client";

import { useEffect, useState } from "react";
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
  ZAxis,
} from "recharts";
import { api, ApiError } from "@/lib/api";
import type { Ledger, ValidationReport } from "@/lib/types";
import { OP_LABELS } from "@/lib/workflow";
import { Badge, Button, Card, ErrorBanner, SectionTitle, Spinner } from "@/components/ui";
import type { PanelProps } from "./types";

type Row = Record<string, string | number | null>;

const ACCENT = "#2563eb";

export function RunPanel({ sessionId, onAdvance, onJump, onError, readOnly }: PanelProps) {
  const [ledger, setLedger] = useState<Ledger | null>(null);
  const [validation, setValidation] = useState<ValidationReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      api.getLedger(sessionId).catch((e: ApiError) => (e.status === 404 ? null : Promise.reject(e))),
      api.getValidation(sessionId).catch((e: ApiError) => (e.status === 404 ? null : Promise.reject(e))),
    ])
      .then(([l, v]) => {
        setLedger(l);
        setValidation(v);
      })
      .catch((e: ApiError) => onError(e.message))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  const runExecute = async () => {
    setLocalError(null);
    setBusy(true);
    try {
      setLedger(await api.execute(sessionId));
      setValidation(await api.runValidation(sessionId));
      await onAdvance();
    } catch (e) {
      setLocalError(e instanceof ApiError ? e.message : "执行失败。");
    } finally {
      setBusy(false);
    }
  };

  const acknowledge = async () => {
    setLocalError(null);
    setBusy(true);
    try {
      setValidation(await api.acknowledge(sessionId));
    } catch (e) {
      setLocalError(e instanceof ApiError ? e.message : "知悉操作失败。");
    } finally {
      setBusy(false);
    }
  };

  const goInsights = async () => {
    setLocalError(null);
    setBusy(true);
    try {
      await api.generateInsights(sessionId);
      await onAdvance();
      onJump(6);
    } catch (e) {
      setLocalError(e instanceof ApiError ? e.message : "洞察生成失败。");
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <CenterSpinner />;

  if (!ledger) {
    return (
      <Card>
        <SectionTitle
          title="执行分析（Execute）"
          desc="执行前会复核方案哈希与数据快照哈希；数值全部由 pandas 计算引擎按白名单算子逐步产出，LLM 不参与任何计算，完整过程写入计算台账。"
        />
        {localError && <div className="mb-4"><ErrorBanner message={localError} onRetry={runExecute} /></div>}
        <Button loading={busy} onClick={runExecute}>执行已确认的方案</Button>
      </Card>
    );
  }

  const blocked = validation?.overall === "fail";
  const warned = validation?.overall === "warn";
  const canInsight = validation?.overall === "pass" || (validation?.acknowledged ?? false);

  return (
    <div className="space-y-4">
      {localError && <ErrorBanner message={localError} />}

      <Card>
        <SectionTitle title="分析结果" desc={ledger.question} />
        <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-zinc-400">
          <Badge tone="blue">图表：{ledger.chart}</Badge>
          <span>快照 {ledger.snapshot_rows.toLocaleString()} 行，参与计算 {ledger.participating_rows.toLocaleString()} 行</span>
          <span>· 结果共 {ledger.result_rows_total.toLocaleString()} 行</span>
          <span>· 耗时 {ledger.elapsed_ms.toFixed(1)} ms</span>
        </div>
        <ResultChart ledger={ledger} />
      </Card>

      {validation && <ValidationCard report={validation} busy={busy} onAck={acknowledge} />}

      {blocked && (
        <Card className="border-red-200 bg-red-50/60">
          <p className="text-sm font-medium text-red-800">校验未通过，已阻断可视化与洞察生成。</p>
          <p className="mt-1 text-xs text-red-600">请返回调整分析方案或数据处理方式后重新执行。</p>
          {!readOnly && (
            <Button variant="secondary" className="mt-3" onClick={() => onJump(4)}>
              ← 返回修改方案
            </Button>
          )}
        </Card>
      )}

      {warned && validation && !validation.acknowledged && (
        <Card className="border-amber-200 bg-amber-50/60">
          <p className="text-sm font-medium text-amber-800">校验存在警告项，需要你知悉后才能继续生成洞察。</p>
          {!readOnly && (
            <Button className="mt-3" loading={busy} onClick={acknowledge}>我已知悉上述警告，继续</Button>
          )}
        </Card>
      )}

      {canInsight && validation && !readOnly && (
        <Card className="flex flex-wrap items-center justify-between gap-3 border-emerald-200 bg-emerald-50/50">
          <p className="text-sm font-medium text-emerald-800">
            {validation.overall === "pass" ? "五项校验全部通过。" : "警告已知悉。"}可以生成业务洞察。
          </p>
          <Button loading={busy} onClick={goInsights}>生成业务洞察 →</Button>
        </Card>
      )}

      <LedgerCard ledger={ledger} />
    </div>
  );
}

// ---------------------------------------------------------------- 图表

function ResultChart({ ledger }: { ledger: Ledger }) {
  const rows = ledger.result_preview as Row[];
  const cols = ledger.result_columns;
  const chart = ledger.chart;

  if (rows.length === 0) {
    return <p className="text-sm text-zinc-400">计算结果为空（可能筛选条件过严）。</p>;
  }

  if (chart === "metric" || chart === "metric_compare") {
    return <MetricView rows={rows} />;
  }

  const dimCol = cols.find((c) => !["value", "share", "is_outlier"].includes(c)) ?? cols[0];

  if (chart === "bar" || chart === "grouped_bar" || chart === "share_bar") {
    return <Bars rows={rows} xKey={dimCol} yKey="value" />;
  }
  if (chart === "pie") {
    return <Pies rows={rows} nameKey={dimCol} />;
  }
  if (chart === "line") {
    return <Lines rows={rows} xKey={dimCol} />;
  }
  if (chart === "line_outlier") {
    return <Lines rows={rows} xKey={dimCol} withOutlier />;
  }
  if (chart === "scatter") {
    const xs = cols.filter((c) => c !== "is_outlier");
    return <Scatters rows={rows} xKey={xs[0]} yKey={xs[1]} />;
  }
  return <PreviewTable rows={rows} cols={cols} />;
}

function ChartFrame({ children, height = 320 }: { children: React.ReactElement; height?: number }) {
  return (
    <div style={{ width: "100%", height }}>
      <ResponsiveContainer>{children}</ResponsiveContainer>
    </div>
  );
}

function MetricView({ rows }: { rows: Row[] }) {
  const v = rows[0].value;
  const formatted = typeof v === "number" ? formatNumber(v) : String(v ?? "—");
  return (
    <div className="rounded-xl bg-gradient-to-br from-accent-soft to-white px-8 py-10 text-center">
      <p className="text-xs font-medium uppercase tracking-wide text-zinc-400">聚合结果</p>
      <p className="mt-2 text-4xl font-semibold tabular-nums text-zinc-900">{formatted}</p>
      <p className="mt-2 text-xs text-zinc-400">由计算引擎直接产出，非 LLM 生成</p>
    </div>
  );
}

function Bars({ rows, xKey, yKey }: { rows: Row[]; xKey: string; yKey: string }) {
  const data = rows.slice(0, 50).map((r) => ({ name: String(r[xKey]), value: Number(r[yKey] ?? 0) }));
  return (
    <ChartFrame>
      <BarChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
        <XAxis dataKey="name" tick={{ fontSize: 11, fill: "#71717a" }} interval="preserveStartEnd" />
        <YAxis tick={{ fontSize: 11, fill: "#71717a" }} tickFormatter={formatNumber} />
        <Tooltip formatter={(v: number) => [formatNumber(v), "数值"]} />
        <Bar dataKey="value" fill={ACCENT} radius={[4, 4, 0, 0]} />
      </BarChart>
    </ChartFrame>
  );
}

function Pies({ rows, nameKey }: { rows: Row[]; nameKey: string }) {
  const data = rows.slice(0, 12).map((r) => ({
    name: String(r[nameKey]),
    value: Number(r.share != null ? Number(r.share) * 100 : r.value ?? 0),
  }));
  const COLORS = ["#2563eb", "#10b981", "#8b5cf6", "#f59e0b", "#ef4444", "#06b6d4", "#ec4899"];
  return (
    <ChartFrame>
      <PieChart>
        <Pie data={data} dataKey="value" nameKey="name" outerRadius={110} label={(e) => e.name}>
          {data.map((_, i) => (
            <Cell key={i} fill={COLORS[i % COLORS.length]} />
          ))}
        </Pie>
        <Tooltip formatter={(v: number) => [`${formatNumber(v)}%`, "占比"]} />
      </PieChart>
    </ChartFrame>
  );
}

function Lines({ rows, xKey, withOutlier = false }: { rows: Row[]; xKey: string; withOutlier?: boolean }) {
  const data = rows.slice(0, 200).map((r) => ({
    name: String(r[xKey]),
    value: r.value === null ? null : Number(r.value),
    outlier: withOutlier && r.is_outlier ? Number(r.value) : null,
  }));
  return (
    <ChartFrame>
      <LineChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
        <XAxis dataKey="name" tick={{ fontSize: 11, fill: "#71717a" }} minTickGap={24} />
        <YAxis tick={{ fontSize: 11, fill: "#71717a" }} tickFormatter={formatNumber} />
        <Tooltip formatter={(v: number) => [formatNumber(v), "数值"]} />
        <Line type="monotone" dataKey="value" stroke={ACCENT} strokeWidth={2} dot={false} connectNulls />
        {withOutlier && (
          <Line type="monotone" dataKey="outlier" stroke="#ef4444" strokeWidth={0}
            dot={{ r: 4, fill: "#ef4444" }} connectNulls />
        )}
      </LineChart>
    </ChartFrame>
  );
}

function Scatters({ rows, xKey, yKey }: { rows: Row[]; xKey: string; yKey: string }) {
  const data = rows.slice(0, 500).map((r) => ({ x: Number(r[xKey]), y: Number(r[yKey]) }));
  return (
    <ChartFrame>
      <ScatterChart margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
        <XAxis type="number" dataKey="x" name={xKey} tick={{ fontSize: 11, fill: "#71717a" }} />
        <YAxis type="number" dataKey="y" name={yKey} tick={{ fontSize: 11, fill: "#71717a" }} />
        <ZAxis range={[32, 32]} />
        <Tooltip cursor={{ strokeDasharray: "3 3" }} />
        <Scatter data={data} fill={ACCENT} />
      </ScatterChart>
    </ChartFrame>
  );
}

function formatNumber(v: number): string {
  if (Math.abs(v) >= 10000) return v.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  return Number(v.toFixed(2)).toString();
}

// ---------------------------------------------------------------- 校验与台账

function ValidationCard({
  report,
  busy,
  onAck,
}: {
  report: ValidationReport;
  busy: boolean;
  onAck: () => void;
}) {
  const tone = report.overall === "pass" ? "green" : report.overall === "warn" ? "amber" : "red";
  const label = report.overall === "pass" ? "全部通过" : report.overall === "warn" ? "存在警告" : "未通过";
  return (
    <Card>
      <div className="mb-3 flex items-center gap-2">
        <SectionTitle title="结果校验（Validate）" desc="五项确定性检查，全部由引擎产物与快照重算完成，LLM 不参与。" />
      </div>
      <div className="mb-4">
        <Badge tone={tone}>{label}{report.acknowledged ? "（已知悉）" : ""}</Badge>
      </div>
      <ul className="space-y-2">
        {report.items.map((item) => (
          <li key={item.code} className="rounded-lg border border-zinc-200 p-3">
            <div className="flex items-center gap-2">
              <span className={`h-2 w-2 rounded-full ${
                item.level === "pass" ? "bg-emerald-500" : item.level === "warn" ? "bg-amber-500" : "bg-red-500"
              }`} />
              <span className="text-sm font-medium text-zinc-800">{item.title}</span>
              <Badge tone={item.level === "pass" ? "green" : item.level === "warn" ? "amber" : "red"}>
                {item.level === "pass" ? "通过" : item.level === "warn" ? "警告" : "失败"}
              </Badge>
            </div>
            <p className="mt-1 text-xs text-zinc-500">{item.detail}</p>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function LedgerCard({ ledger }: { ledger: Ledger }) {
  return (
    <Card>
      <SectionTitle title="计算台账（可追溯证据）" desc="每一步的口径、公式、参与行数与中间结果均留痕。" />
      <div className="space-y-2">
        {ledger.steps.map((s, i) => (
          <div key={s.step_id} className="rounded-lg border border-zinc-200 p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="flex h-5 w-5 items-center justify-center rounded-full bg-zinc-800 text-[10px] text-white">
                {i + 1}
              </span>
              <Badge tone="blue">{OP_LABELS[s.op] ?? s.op}</Badge>
              <span className="font-mono text-xs text-zinc-400">{s.step_id}</span>
              <span className="ml-auto text-xs text-zinc-400">
                {s.input_rows.toLocaleString()} → {s.output_rows.toLocaleString()} 行
              </span>
            </div>
            <p className="mt-2 text-zinc-700">{s.description}</p>
            <p className="mt-1 font-mono text-xs text-zinc-500">{s.formula}</p>
            {Object.keys(s.summary).length > 0 && (
              <p className="mt-1 text-xs text-zinc-400">
                中间结果：{Object.entries(s.summary).map(([k, v]) => `${k}=${String(v)}`).join("，")}
              </p>
            )}
          </div>
        ))}
      </div>

      <details className="mt-4">
        <summary className="cursor-pointer text-xs text-zinc-400">
          查看结果明细（前 {Math.min(ledger.result_preview.length, 1000)} 行）
        </summary>
        <div className="mt-2">
          <PreviewTable rows={ledger.result_preview as Row[]} cols={ledger.result_columns} />
        </div>
      </details>
    </Card>
  );
}

function PreviewTable({ rows, cols }: { rows: Row[]; cols: string[] }) {
  return (
    <div className="max-h-80 overflow-auto rounded-lg border border-zinc-200">
      <table className="w-full border-collapse text-xs">
        <thead className="sticky top-0 bg-white">
          <tr>
            {cols.map((c) => (
              <th key={c} className="whitespace-nowrap border-b border-zinc-200 px-3 py-2 text-left font-medium text-zinc-500">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 200).map((row, i) => (
            <tr key={i} className="even:bg-zinc-50">
              {cols.map((c) => (
                <td key={c} className="whitespace-nowrap px-3 py-1.5 text-zinc-700">
                  {row[c] === null || row[c] === "" ? "—" : String(row[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CenterSpinner() {
  return (
    <div className="flex items-center justify-center py-20 text-zinc-400">
      <Spinner />
    </div>
  );
}
