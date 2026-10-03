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
} from "recharts";
import { api, ApiError } from "@/lib/api";
import type { Ledger, ValidationReport } from "@/lib/types";
import { OP_LABELS } from "@/lib/workflow";
import {
  Badge,
  Button,
  Card,
  ErrorBanner,
  SectionTitle,
  Spinner,
} from "@/components/ui";
import type { PanelProps } from "./types";

type Row = Record<string, string | number | null>;

const BLUE = "#0071e3";
const GRID = "#f0f0f2";
const AXIS = "#86868b";

// Apple 系统色序列
const PIE_COLORS = [
  "#0071e3",
  "#7c4dff",
  "#ff9500",
  "#34c759",
  "#ff3b30",
  "#5ac8fa",
  "#ff2d55",
  "#af52de",
  "#ffcc00",
  "#64d2ff",
];

const tooltipStyle = {
  borderRadius: 14,
  border: "1px solid #ebebed",
  boxShadow: "0 8px 28px rgba(0,0,0,0.10)",
  background: "rgba(255,255,255,0.92)",
  backdropFilter: "blur(12px)",
  fontSize: 12,
  padding: "8px 12px",
} as const;

export function RunPanel({
  sessionId,
  onAdvance,
  onJump,
  onError,
  readOnly,
}: PanelProps) {
  const [ledger, setLedger] = useState<Ledger | null>(null);
  const [validation, setValidation] = useState<ValidationReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      api
        .getLedger(sessionId)
        .catch((e: ApiError) => (e.status === 404 ? null : Promise.reject(e))),
      api
        .getValidation(sessionId)
        .catch((e: ApiError) => (e.status === 404 ? null : Promise.reject(e))),
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
        {localError && (
          <div className="mb-5">
            <ErrorBanner message={localError} onRetry={runExecute} />
          </div>
        )}
        <Button loading={busy} onClick={runExecute}>
          执行已确认的方案
        </Button>
      </Card>
    );
  }

  const blocked = validation?.overall === "fail";
  const warned = validation?.overall === "warn";
  const canInsight =
    validation?.overall === "pass" || (validation?.acknowledged ?? false);

  return (
    <div className="space-y-5">
      {localError && <ErrorBanner message={localError} />}

      <Card>
        <SectionTitle title="分析结果" desc={ledger.question} />
        <div className="mb-5 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-faint">
          <Badge tone="blue">图表：{ledger.chart}</Badge>
          <span>快照 {ledger.snapshot_rows.toLocaleString()} 行</span>
          <span>参与计算 {ledger.participating_rows.toLocaleString()} 行</span>
          <span>结果 {ledger.result_rows_total.toLocaleString()} 行</span>
          <span>耗时 {ledger.elapsed_ms.toFixed(1)} ms</span>
        </div>
        <ResultChart ledger={ledger} />
      </Card>

      {validation && (
        <ValidationCard report={validation} />
      )}

      {blocked && (
        <div className="rounded-3xl border border-danger/20 bg-danger-soft px-7 py-6">
          <p className="text-sm font-semibold text-danger">
            校验未通过，已阻断可视化与洞察生成。
          </p>
          <p className="mt-1 text-xs text-danger/80">
            请返回调整分析方案或数据处理方式后重新执行。
          </p>
          {!readOnly && (
            <Button
              variant="secondary"
              className="mt-4"
              onClick={() => onJump(4)}
            >
              ← 返回修改方案
            </Button>
          )}
        </div>
      )}

      {warned && validation && !validation.acknowledged && (
        <div className="rounded-3xl border border-warning/25 bg-warning-soft px-7 py-6">
          <p className="text-sm font-semibold text-warning">
            校验存在警告项，需要你知悉后才能继续生成洞察。
          </p>
          {!readOnly && (
            <Button className="mt-4" loading={busy} onClick={acknowledge}>
              我已知悉上述警告，继续
            </Button>
          )}
        </div>
      )}

      {canInsight && validation && !readOnly && (
        <div className="flex flex-wrap items-center justify-between gap-4 rounded-3xl border border-success/20 bg-success-soft px-7 py-6">
          <p className="text-sm font-semibold text-success">
            {validation.overall === "pass"
              ? "五项校验全部通过。"
              : "警告已知悉。"}
            可以生成业务洞察。
          </p>
          <Button loading={busy} onClick={goInsights}>
            生成业务洞察 →
          </Button>
        </div>
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
    return (
      <p className="rounded-2xl bg-canvas px-6 py-10 text-center text-sm text-muted">
        计算结果为空（可能筛选条件过严）。
      </p>
    );
  }

  if (chart === "metric" || chart === "metric_compare") {
    return <MetricView rows={rows} />;
  }

  const dimCol =
    cols.find((c) => !["value", "share", "is_outlier"].includes(c)) ?? cols[0];

  if (chart === "bar" || chart === "grouped_bar" || chart === "share_bar") {
    return <Bars rows={rows} xKey={dimCol} yKey="value" />;
  }
  if (chart === "pie") return <Pies rows={rows} nameKey={dimCol} />;
  if (chart === "line") return <Lines rows={rows} xKey={dimCol} />;
  if (chart === "line_outlier")
    return <Lines rows={rows} xKey={dimCol} withOutlier />;
  if (chart === "scatter") {
    const xs = cols.filter((c) => c !== "is_outlier");
    return <Scatters rows={rows} xKey={xs[0]} yKey={xs[1]} />;
  }
  return <PreviewTable rows={rows} cols={cols} embedded />;
}

function ChartFrame({
  children,
  height = 340,
}: {
  children: React.ReactElement;
  height?: number;
}) {
  return (
    <div style={{ width: "100%", height }}>
      <ResponsiveContainer>{children}</ResponsiveContainer>
    </div>
  );
}

function MetricView({ rows }: { rows: Row[] }) {
  const v = rows[0].value;
  const formatted =
    typeof v === "number" ? formatNumber(v) : String(v ?? "—");
  return (
    <div className="rounded-3xl bg-canvas px-8 py-14 text-center">
      <p className="text-xs font-medium uppercase tracking-[0.14em] text-faint">
        聚合结果
      </p>
      <p className="mt-4 text-6xl font-semibold tabular-nums tracking-tight text-ink">
        {formatted}
      </p>
      <p className="mt-4 text-xs text-faint">
        由计算引擎直接产出，非 LLM 生成
      </p>
    </div>
  );
}

function axisTick(fontSize = 12) {
  return { fontSize, fill: AXIS };
}

function Bars({
  rows,
  xKey,
  yKey,
}: {
  rows: Row[];
  xKey: string;
  yKey: string;
}) {
  const data = rows
    .slice(0, 50)
    .map((r) => ({ name: String(r[xKey]), value: Number(r[yKey] ?? 0) }));
  return (
    <ChartFrame>
      <BarChart
        data={data}
        margin={{ top: 8, right: 16, bottom: 8, left: 8 }}
      >
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis
          dataKey="name"
          tick={axisTick(11)}
          axisLine={{ stroke: GRID }}
          tickLine={false}
        />
        <YAxis
          tick={axisTick(11)}
          axisLine={false}
          tickLine={false}
          tickFormatter={formatNumber}
        />
        <Tooltip
          formatter={(v: number) => [formatNumber(v), "数值"]}
          contentStyle={tooltipStyle}
          cursor={{ fill: "rgba(0,113,227,0.06)" }}
        />
        <Bar dataKey="value" fill={BLUE} radius={[6, 6, 0, 0]} maxBarSize={56} />
      </BarChart>
    </ChartFrame>
  );
}

function Pies({ rows, nameKey }: { rows: Row[]; nameKey: string }) {
  const data = rows.slice(0, 12).map((r) => ({
    name: String(r[nameKey]),
    value: Number(
      r.share != null ? Number(r.share) * 100 : r.value ?? 0,
    ),
  }));
  return (
    <ChartFrame height={360}>
      <PieChart>
        <Pie
          data={data}
          dataKey="value"
          nameKey="name"
          outerRadius={128}
          innerRadius={64}
          paddingAngle={2}
          stroke="none"
        >
          {data.map((_, i) => (
            <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
          ))}
        </Pie>
        <Tooltip
          formatter={(v: number) => [`${formatNumber(v)}%`, "占比"]}
          contentStyle={tooltipStyle}
        />
      </PieChart>
    </ChartFrame>
  );
}

function Lines({
  rows,
  xKey,
  withOutlier = false,
}: {
  rows: Row[];
  xKey: string;
  withOutlier?: boolean;
}) {
  const data = rows.slice(0, 200).map((r) => ({
    name: String(r[xKey]),
    value: r.value === null ? null : Number(r.value),
    outlier: withOutlier && r.is_outlier ? Number(r.value) : null,
  }));
  return (
    <ChartFrame>
      <LineChart
        data={data}
        margin={{ top: 8, right: 16, bottom: 8, left: 8 }}
      >
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis
          dataKey="name"
          tick={axisTick(11)}
          axisLine={{ stroke: GRID }}
          tickLine={false}
          minTickGap={24}
        />
        <YAxis
          tick={axisTick(11)}
          axisLine={false}
          tickLine={false}
          tickFormatter={formatNumber}
        />
        <Tooltip
          formatter={(v: number) => [formatNumber(v), "数值"]}
          contentStyle={tooltipStyle}
        />
        <Line
          type="monotone"
          dataKey="value"
          stroke={BLUE}
          strokeWidth={2.5}
          dot={false}
          activeDot={{ r: 5, strokeWidth: 0 }}
          connectNulls
        />
        {withOutlier && (
          <Line
            type="monotone"
            dataKey="outlier"
            stroke="#ff3b30"
            strokeWidth={0}
            dot={{ r: 5, fill: "#ff3b30", strokeWidth: 0 }}
            connectNulls
          />
        )}
      </LineChart>
    </ChartFrame>
  );
}

function Scatters({
  rows,
  xKey,
  yKey,
}: {
  rows: Row[];
  xKey: string;
  yKey: string;
}) {
  const data = rows
    .slice(0, 500)
    .map((r) => ({ x: Number(r[xKey]), y: Number(r[yKey]) }));
  return (
    <ChartFrame>
      <ScatterChart
        margin={{ top: 8, right: 16, bottom: 8, left: 8 }}
      >
        <CartesianGrid stroke={GRID} />
        <XAxis
          type="number"
          dataKey="x"
          name={xKey}
          tick={axisTick(11)}
          axisLine={{ stroke: GRID }}
          tickLine={false}
        />
        <YAxis
          type="number"
          dataKey="y"
          name={yKey}
          tick={axisTick(11)}
          axisLine={false}
          tickLine={false}
        />
        <Tooltip
          cursor={{ strokeDasharray: "3 3", stroke: AXIS }}
          contentStyle={tooltipStyle}
        />
        <Scatter data={data} fill={BLUE} />
      </ScatterChart>
    </ChartFrame>
  );
}

function formatNumber(v: number): string {
  if (Math.abs(v) >= 10000)
    return v.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  return Number(v.toFixed(2)).toString();
}

// ---------------------------------------------------------------- 校验

function ValidationCard({ report }: { report: ValidationReport }) {
  const tone =
    report.overall === "pass"
      ? "green"
      : report.overall === "warn"
        ? "amber"
        : "red";
  const label =
    report.overall === "pass"
      ? "全部通过"
      : report.overall === "warn"
        ? "存在警告"
        : "未通过";
  return (
    <Card>
      <SectionTitle
        title="结果校验（Validate）"
        desc="五项确定性检查，全部由引擎产物与快照重算完成，LLM 不参与。"
      />
      <div className="mb-5">
        <Badge tone={tone}>
          {label}
          {report.acknowledged ? "（已知悉）" : ""}
        </Badge>
      </div>
      <ul className="space-y-3">
        {report.items.map((item) => (
          <li
            key={item.code}
            className="rounded-2xl border border-hairline bg-canvas/40 p-4"
          >
            <div className="flex items-center gap-2.5">
              <span
                className={`h-2 w-2 rounded-full ${
                  item.level === "pass"
                    ? "bg-success.dot"
                    : item.level === "warn"
                      ? "bg-warning.dot"
                      : "bg-danger.dot"
                }`}
              />
              <span className="text-sm font-medium text-ink">
                {item.title}
              </span>
              <Badge
                tone={
                  item.level === "pass"
                    ? "green"
                    : item.level === "warn"
                      ? "amber"
                      : "red"
                }
              >
                {item.level === "pass"
                  ? "通过"
                  : item.level === "warn"
                    ? "警告"
                    : "失败"}
              </Badge>
            </div>
            <p className="mt-2 text-xs leading-relaxed text-muted">
              {item.detail}
            </p>
          </li>
        ))}
      </ul>
    </Card>
  );
}

// ---------------------------------------------------------------- 台账

function LedgerCard({ ledger }: { ledger: Ledger }) {
  return (
    <Card>
      <SectionTitle
        title="计算台账（可追溯证据）"
        desc="每一步的口径、公式、参与行数与中间结果均留痕。"
      />
      <div className="space-y-3">
        {ledger.steps.map((s, i) => (
          <div
            key={s.step_id}
            className="rounded-2xl border border-hairline bg-canvas/40 p-5 text-sm"
          >
            <div className="flex flex-wrap items-center gap-2.5">
              <span className="flex h-5 w-5 items-center justify-center rounded-full bg-ink text-[10px] font-semibold text-white">
                {i + 1}
              </span>
              <Badge tone="blue">{OP_LABELS[s.op] ?? s.op}</Badge>
              <span className="font-mono text-xs text-faint">{s.step_id}</span>
              <span className="ml-auto text-xs text-faint">
                {s.input_rows.toLocaleString()} →{" "}
                {s.output_rows.toLocaleString()} 行
              </span>
            </div>
            <p className="mt-3 text-ink/80">{s.description}</p>
            <p className="mt-1.5 font-mono text-xs text-muted">{s.formula}</p>
            {Object.keys(s.summary).length > 0 && (
              <p className="mt-2 text-xs text-faint">
                中间结果：
                {Object.entries(s.summary)
                  .map(([k, v]) => `${k}=${String(v)}`)
                  .join("，")}
              </p>
            )}
          </div>
        ))}
      </div>

      <details className="group mt-5">
        <summary className="flex cursor-pointer list-none items-center gap-2 text-xs font-medium text-muted hover:text-ink">
          查看结果明细（前 {Math.min(ledger.result_preview.length, 1000)} 行）
          <svg
            className="h-3.5 w-3.5 transition-transform group-open:rotate-180"
            viewBox="0 0 20 20"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M5 8l5 5 5-5" />
          </svg>
        </summary>
        <div className="mt-3">
          <PreviewTable
            rows={ledger.result_preview as Row[]}
            cols={ledger.result_columns}
          />
        </div>
      </details>
    </Card>
  );
}

function PreviewTable({
  rows,
  cols,
  embedded = false,
}: {
  rows: Row[];
  cols: string[];
  embedded?: boolean;
}) {
  return (
    <div
      className={`max-h-96 overflow-auto ${
        embedded ? "rounded-2xl border border-hairline" : ""
      }`}
    >
      <table className="w-full border-collapse text-xs">
        <thead className="sticky top-0 z-10">
          <tr>
            {cols.map((c) => (
              <th
                key={c}
                className="whitespace-nowrap border-b border-hairline bg-canvas px-4 py-2.5 text-left font-medium text-muted"
              >
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 200).map((row, i) => (
            <tr key={i} className="odd:bg-white even:bg-canvas/30">
              {cols.map((c) => (
                <td
                  key={c}
                  className="whitespace-nowrap px-4 py-2 text-ink/80"
                >
                  {row[c] === null || row[c] === "" ? (
                    <span className="text-faint">—</span>
                  ) : (
                    String(row[c])
                  )}
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
    <div className="flex items-center justify-center py-24 text-muted">
      <Spinner className="h-6 w-6 text-apple" />
    </div>
  );
}
