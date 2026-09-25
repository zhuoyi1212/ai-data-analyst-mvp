"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { DataDictionary, PlanArtifact, PlanStep } from "@/lib/types";
import { OP_LABELS } from "@/lib/workflow";
import { Badge, Button, Card, ErrorBanner, SectionTitle, Spinner } from "@/components/ui";
import type { PanelProps } from "./types";

const FUNCS = ["sum", "mean", "median", "count", "count_distinct", "min", "max"];
const FUNC_LABELS: Record<string, string> = {
  sum: "求和", mean: "平均值", median: "中位数", count: "计数",
  count_distinct: "去重计数", min: "最小值", max: "最大值",
};
const GRANULARITIES = ["day", "week", "month", "quarter", "year"];
const GRAN_LABELS: Record<string, string> = {
  day: "按天", week: "按周", month: "按月", quarter: "按季", year: "按年",
};
// 可在 UI 安全调整参数的算子（其余算子只读，防止越出白名单的任意编辑）
const EDITABLE_OPS = new Set(["aggregate", "group_by", "top_n", "share", "time_series"]);

export function PlanPanel({ sessionId, onAdvance, onJump, onError, readOnly }: PanelProps) {
  const [artifact, setArtifact] = useState<PlanArtifact | null>(null);
  const [dictionary, setDictionary] = useState<DataDictionary | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [draft, setDraft] = useState<PlanArtifact["plan"] | null>(null);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    Promise.all([
      api.getPlan(sessionId).catch((e: ApiError) => {
        if (e.status === 404) return null;
        throw e;
      }),
      api.getProfile(sessionId).catch(() => null),
    ])
      .then(([plan, prof]) => {
        setArtifact(plan);
        if (plan) setDraft(structuredClone(plan.plan));
        if (prof) setDictionary(prof.dictionary);
      })
      .catch((e: ApiError) => onError(e.message))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  const metrics = (dictionary?.fields ?? []).filter((f) => f.semantic_type === "metric");
  const dims = (dictionary?.fields ?? []).filter(
    (f) => f.semantic_type === "dimension" || f.semantic_type === "geo",
  );
  const dates = (dictionary?.fields ?? []).filter((f) => f.semantic_type === "date");

  const patchStep = (idx: number, patch: Record<string, unknown>) => {
    if (!draft) return;
    const steps = structuredClone(draft.steps);
    steps[idx] = { ...steps[idx], params: { ...steps[idx].params, ...patch } };
    setDraft({ ...draft, steps });
    setDirty(true);
  };

  const saveEdit = async () => {
    if (!draft) return;
    setLocalError(null);
    setBusy(true);
    try {
      const a = await api.editPlan(sessionId, draft);
      setArtifact(a);
      setDraft(structuredClone(a.plan));
      setDirty(false);
    } catch (e) {
      setLocalError(e instanceof ApiError ? e.message : "方案校验未通过。");
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    setLocalError(null);
    setBusy(true);
    try {
      const a = await api.confirmPlan(sessionId);
      setArtifact(a);
      await onAdvance();
      onJump(5);
    } catch (e) {
      setLocalError(e instanceof ApiError ? e.message : "方案确认失败。");
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <CenterSpinner />;

  if (!artifact || !draft) {
    return (
      <Card>
        <SectionTitle title="分析方案（Analysis Plan）" desc="请先返回上一步选择或输入一个问题，系统将把问题翻译为受支持算子的结构化方案。" />
        {!readOnly && <Button variant="secondary" onClick={() => onJump(3)}>← 返回选择问题</Button>}
      </Card>
    );
  }

  const locked = artifact.locked;

  return (
    <div className="space-y-4">
      <Card>
        <SectionTitle
          title="分析方案确认"
          desc="方案仅由 10 个白名单算子构成，不生成任何自由代码；服务端会对字段语义与依赖关系重新校验。你可以调整参数后再确认。"
        />
        {localError && <div className="mb-4"><ErrorBanner message={localError} /></div>}

        <div className="mb-4 flex flex-wrap items-center gap-2">
          {locked ? (
            <Badge tone="green">已确认锁定 · 哈希 {artifact.plan_hash?.slice(0, 8)}</Badge>
          ) : (
            <Badge tone="amber">待确认</Badge>
          )}
          <Badge>推荐图表：{artifact.chart}</Badge>
        </div>

        <div className="space-y-3">
          <Field label="分析问题">
            <input
              value={draft.question}
              disabled={locked}
              onChange={(e) => {
                setDraft({ ...draft, question: e.target.value });
                setDirty(true);
              }}
              className="w-full rounded-md border border-zinc-300 px-3 py-2 text-sm disabled:bg-zinc-50"
            />
          </Field>
          <Field label="数据范围">
            <input
              value={draft.data_scope}
              disabled={locked}
              onChange={(e) => {
                setDraft({ ...draft, data_scope: e.target.value });
                setDirty(true);
              }}
              className="w-full rounded-md border border-zinc-300 px-3 py-2 text-sm disabled:bg-zinc-50"
            />
          </Field>
        </div>

        <div className="mt-5 space-y-3">
          {draft.steps.map((step, idx) => (
            <StepCard
              key={step.step_id}
              step={step}
              index={idx}
              locked={locked}
              metrics={metrics.map((f) => f.name)}
              dims={dims.map((f) => f.name)}
              dates={dates.map((f) => f.name)}
              onPatch={(p) => patchStep(idx, p)}
            />
          ))}
        </div>

        {!locked && (
          <div className="mt-5 flex flex-wrap items-center gap-3">
            <Button variant="secondary" loading={busy} disabled={!dirty} onClick={saveEdit}>
              保存调整并重新校验
            </Button>
            <Button loading={busy} disabled={dirty} onClick={confirm}>
              {dirty ? "请先保存调整" : "确认方案并执行 →"}
            </Button>
            <span className="text-xs text-zinc-400">调整后必须通过同一套服务端校验才能确认</span>
          </div>
        )}
        {locked && !readOnly && (
          <div className="mt-5">
            <Button loading={busy} onClick={() => onJump(5)}>进入执行与校验 →</Button>
          </div>
        )}
      </Card>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-zinc-500">{label}</span>
      {children}
    </label>
  );
}

function StepCard({
  step,
  index,
  locked,
  metrics,
  dims,
  dates,
  onPatch,
}: {
  step: PlanStep;
  index: number;
  locked: boolean;
  metrics: string[];
  dims: string[];
  dates: string[];
  onPatch: (patch: Record<string, unknown>) => void;
}) {
  const editable = EDITABLE_OPS.has(step.op) && !locked;
  return (
    <div className="rounded-xl border border-zinc-200 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-zinc-900 text-xs font-semibold text-white">
          {index + 1}
        </span>
        <Badge tone="blue">{OP_LABELS[step.op] ?? step.op}</Badge>
        <span className="font-mono text-xs text-zinc-400">{step.step_id}</span>
        {step.depends_on.length > 0 && (
          <span className="text-xs text-zinc-400">依赖：{step.depends_on.join("、")}</span>
        )}
        {!editable && !locked && (
          <span className="text-xs text-zinc-400">（该算子参数为只读，避免任意编辑风险）</span>
        )}
      </div>
      <p className="mt-2 text-sm text-zinc-700">{step.description}</p>

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        {typeof step.params.column === "string" && (
          <ParamSelect
            label="指标字段"
            value={step.params.column as string}
            options={metrics}
            disabled={!editable}
            onChange={(v) => onPatch({ column: v })}
          />
        )}
        {typeof step.params.metric === "string" && (
          <ParamSelect
            label="度量指标"
            value={step.params.metric as string}
            options={metrics}
            disabled={!editable}
            onChange={(v) => onPatch({ metric: v })}
          />
        )}
        {typeof step.params.dimension === "string" && (
          <ParamSelect
            label="分组维度"
            value={step.params.dimension as string}
            options={dims}
            disabled={!editable}
            onChange={(v) => onPatch({ dimension: v })}
          />
        )}
        {typeof step.params.date_column === "string" && (
          <ParamSelect
            label="日期字段"
            value={step.params.date_column as string}
            options={dates}
            disabled={!editable}
            onChange={(v) => onPatch({ date_column: v })}
          />
        )}
        {typeof step.params.func === "string" && (
          <ParamSelect
            label="计算方法"
            value={step.params.func as string}
            options={FUNCS}
            labels={FUNC_LABELS}
            disabled={!editable}
            onChange={(v) => onPatch({ func: v })}
          />
        )}
        {typeof step.params.granularity === "string" && (
          <ParamSelect
            label="时间粒度"
            value={step.params.granularity as string}
            options={GRANULARITIES}
            labels={GRAN_LABELS}
            disabled={!editable}
            onChange={(v) => onPatch({ granularity: v })}
          />
        )}
      </div>
    </div>
  );
}

function ParamSelect({
  label,
  value,
  options,
  labels,
  disabled,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  labels?: Record<string, string>;
  disabled: boolean;
  onChange: (v: string) => void;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs text-zinc-500">{label}</span>
      <select
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-md border border-zinc-300 px-2 py-1.5 text-sm disabled:bg-zinc-50"
      >
        {options.map((o) => (
          <option key={o} value={o}>
            {labels?.[o] ?? o}
          </option>
        ))}
      </select>
    </label>
  );
}

function CenterSpinner() {
  return (
    <div className="flex items-center justify-center py-20 text-zinc-400">
      <Spinner />
    </div>
  );
}
