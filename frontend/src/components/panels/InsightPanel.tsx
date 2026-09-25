"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { FollowUpQuestion, GroundedNumber, Insight, Ledger } from "@/lib/types";
import { OP_LABELS } from "@/lib/workflow";
import { Badge, Button, Card, ErrorBanner, SectionTitle, Spinner } from "@/components/ui";
import type { PanelProps } from "./types";

const CONF_TONE = { high: "green", medium: "amber", low: "red" } as const;
const CONF_LABEL = { high: "高可信", medium: "中可信", low: "需谨慎" } as const;

export function InsightPanel({ sessionId, onAdvance, onJump, onError, readOnly }: PanelProps) {
  const [insights, setInsights] = useState<Insight[] | null>(null);
  const [followups, setFollowups] = useState<FollowUpQuestion[] | null>(null);
  const [ledger, setLedger] = useState<Ledger | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [evidenceFor, setEvidenceFor] = useState<Insight | null>(null);

  useEffect(() => {
    Promise.all([
      api.getInsights(sessionId).catch((e: ApiError) => (e.status === 404 ? null : Promise.reject(e))),
      api.getFollowups(sessionId).catch((e: ApiError) => (e.status === 404 ? null : Promise.reject(e))),
      api.getLedger(sessionId).catch(() => null),
    ])
      .then(([ins, fu, l]) => {
        setInsights(ins?.insights ?? null);
        setFollowups(fu?.questions ?? null);
        setLedger(l);
      })
      .catch((e: ApiError) => onError(e.message))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  const generateInsights = async () => {
    setLocalError(null);
    setBusy(true);
    try {
      setInsights((await api.generateInsights(sessionId)).insights);
      await onAdvance();
    } catch (e) {
      setLocalError(e instanceof ApiError ? e.message : "洞察生成失败。");
    } finally {
      setBusy(false);
    }
  };

  const generateFollowups = async () => {
    setLocalError(null);
    setBusy(true);
    try {
      setFollowups((await api.generateFollowups(sessionId)).questions);
    } catch (e) {
      setLocalError(e instanceof ApiError ? e.message : "追问生成失败。");
    } finally {
      setBusy(false);
    }
  };

  const askFollowup = async (q: FollowUpQuestion) => {
    setLocalError(null);
    setBusy(true);
    try {
      await api.generatePlan(sessionId, q.text);
      await onAdvance();
      onJump(4);
    } catch (e) {
      setLocalError(e instanceof ApiError ? e.message : "无法为该追问生成方案。");
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <CenterSpinner />;

  return (
    <div className="space-y-4">
      {localError && <ErrorBanner message={localError} />}

      {!insights ? (
        <Card>
          <SectionTitle
            title="业务洞察（Insight）"
            desc="洞察只基于引擎结果摘要撰写，其中每个数字都必须指向具体计算结果；因果性表述被严格禁止，相关分析会强制标注免责声明。"
          />
          {!readOnly && (
            <Button loading={busy} onClick={generateInsights}>生成业务洞察</Button>
          )}
        </Card>
      ) : (
        <>
          <div className="space-y-3">
            {insights.map((ins, i) => (
              <Card key={i}>
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="flex h-6 w-6 items-center justify-center rounded-full bg-zinc-900 text-xs text-white">
                      {i + 1}
                    </span>
                    <Badge tone={CONF_TONE[ins.confidence]}>{CONF_LABEL[ins.confidence]}</Badge>
                    {ins.needs_further_validation && (
                      <Badge tone="amber">建议进一步验证</Badge>
                    )}
                  </div>
                  <Button variant="ghost" className="px-2 py-1 text-xs"
                    onClick={() => setEvidenceFor(ins)}>
                    查看完整证据 ↗
                  </Button>
                </div>
                <p className="mt-3 text-sm leading-6 text-zinc-900">{ins.text}</p>
                {ins.disclaimer && (
                  <p className="mt-2 rounded bg-zinc-50 px-3 py-2 text-xs text-zinc-500">
                    免责声明：{ins.disclaimer}
                  </p>
                )}
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <span className="text-xs text-zinc-400">接地数字：</span>
                  {ins.numbers.map((n, j) => (
                    <span key={j} className="rounded-full bg-accent-soft px-2 py-0.5 text-xs text-accent">
                      {n.label}: {formatValue(n.value)}
                    </span>
                  ))}
                </div>
                <p className="mt-2 text-xs text-zinc-400">可信度依据：{ins.confidence_reason}</p>
              </Card>
            ))}
          </div>

          <Card>
            <SectionTitle
              title="后续可以继续问（Follow-up）"
              desc="追问同样带推荐依据；点击任一问题会立即生成新的结构化方案，形成分析闭环。"
            />
            {!followups ? (
              !readOnly && <Button loading={busy} onClick={generateFollowups}>生成后续问题建议</Button>
            ) : (
              <div className="grid gap-2">
                {followups.map((q, i) => (
                  <button
                    key={i}
                    type="button"
                    disabled={busy}
                    onClick={() => void askFollowup(q)}
                    className="flex items-start justify-between gap-3 rounded-lg border border-zinc-200 px-4 py-3 text-left transition-colors hover:border-accent hover:bg-accent-soft disabled:opacity-50"
                  >
                    <span>
                      <span className="block text-sm font-medium text-zinc-800">{q.text}</span>
                      <span className="mt-1 block text-xs text-zinc-400">依据：{q.rationale}</span>
                      <span className="mt-1 flex flex-wrap gap-1">
                        {q.fields.map((f) => (
                          <span key={f} className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-[11px] text-zinc-500">
                            {f}
                          </span>
                        ))}
                      </span>
                    </span>
                    <span className="shrink-0 text-xs text-accent">分析 →</span>
                  </button>
                ))}
              </div>
            )}
          </Card>
        </>
      )}

      {evidenceFor && ledger && (
        <EvidenceDrawer
          insight={evidenceFor}
          ledger={ledger}
          onClose={() => setEvidenceFor(null)}
        />
      )}
    </div>
  );
}

function formatValue(v: number): string {
  if (Number.isInteger(v)) return v.toLocaleString("zh-CN");
  return v.toLocaleString("zh-CN", { maximumFractionDigits: 3 });
}

// ---------------------------------------------------------------- 证据抽屉

function EvidenceDrawer({
  insight,
  ledger,
  onClose,
}: {
  insight: Insight;
  ledger: Ledger;
  onClose: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/30" onClick={onClose}>
      <aside
        className="h-full w-full max-w-xl overflow-y-auto bg-white p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-start justify-between gap-4">
          <h3 className="text-base font-semibold text-zinc-900">洞察完整证据链</h3>
          <button onClick={onClose} className="text-zinc-400 hover:text-zinc-700">✕</button>
        </div>

        <section className="mb-5">
          <p className="text-xs font-medium text-zinc-400">洞察结论</p>
          <p className="mt-1 text-sm leading-6 text-zinc-900">{insight.text}</p>
          <p className="mt-2 text-xs text-zinc-500">可信度：{CONF_LABEL[insight.confidence]} · {insight.confidence_reason}</p>
        </section>

        <section className="mb-5">
          <p className="mb-2 text-xs font-medium text-zinc-400">数据范围与快照</p>
          <dl className="space-y-1 rounded-lg bg-zinc-50 p-3 text-xs text-zinc-600">
            <Meta k="分析问题" v={ledger.question} />
            <Meta k="数据范围" v={ledger.data_scope || "（未额外筛选）"} />
            <Meta k="快照行数" v={ledger.snapshot_rows.toLocaleString()} />
            <Meta k="参与计算行数" v={ledger.participating_rows.toLocaleString()} />
            <Meta k="快照哈希" v={<span className="font-mono">{ledger.snapshot_hash.slice(0, 24)}…</span>} />
            <Meta k="方案哈希" v={<span className="font-mono">{ledger.plan_hash}</span>} />
          </dl>
        </section>

        <section className="mb-5">
          <p className="mb-2 text-xs font-medium text-zinc-400">
            接地数字（{insight.numbers.length}）→ 对应计算步骤
          </p>
          <div className="space-y-3">
            {insight.numbers.map((n, i) => (
              <NumberEvidence key={i} n={n} ledger={ledger} />
            ))}
          </div>
        </section>

        <section>
          <p className="mb-2 text-xs font-medium text-zinc-400">完整计算台账</p>
          <div className="space-y-2">
            {ledger.steps.map((s, i) => (
              <div key={s.step_id} className="rounded-lg border border-zinc-200 p-3 text-xs">
                <div className="flex items-center gap-2">
                  <span className="flex h-4 w-4 items-center justify-center rounded-full bg-zinc-800 text-[9px] text-white">
                    {i + 1}
                  </span>
                  <Badge tone="blue">{OP_LABELS[s.op] ?? s.op}</Badge>
                  <span className="font-mono text-zinc-400">{s.step_id}</span>
                </div>
                <p className="mt-1 text-zinc-700">{s.description}</p>
                <p className="mt-1 font-mono text-zinc-500">{s.formula}</p>
                <p className="mt-1 text-zinc-400">
                  参数：{JSON.stringify(s.params)}；{s.input_rows}→{s.output_rows} 行
                </p>
              </div>
            ))}
          </div>
        </section>
      </aside>
    </div>
  );
}

function NumberEvidence({ n, ledger }: { n: GroundedNumber; ledger: Ledger }) {
  const step = ledger.steps.find((s) => s.step_id === n.ref_step);
  return (
    <div className="rounded-lg border border-zinc-200 p-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-semibold text-zinc-900">{n.label}</span>
        <span className="text-accent">{formatValue(n.value)}</span>
      </div>
      <dl className="mt-2 space-y-1 text-xs text-zinc-500">
        <Meta k="引用类型" v={n.ref_kind === "metric" ? "指标结果" : n.ref_kind === "row" ? "结果行" : "汇总指标"} />
        <Meta k="定位键" v={<span className="font-mono">{JSON.stringify(n.ref_keys) || "{}"}</span>} />
        {step && <Meta k="步骤口径" v={step.description} />}
        {step && <Meta k="计算公式" v={<span className="font-mono">{step.formula}</span>} />}
        {step && <Meta k="筛选条件/参数" v={<span className="font-mono">{JSON.stringify(step.params)}</span>} />}
        {step && <Meta k="输入行数" v={`${step.input_rows.toLocaleString()} 行`} />}
      </dl>
    </div>
  );
}

function Meta({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex gap-2">
      <dt className="w-24 shrink-0 text-zinc-400">{k}</dt>
      <dd className="min-w-0 flex-1 break-all">{v}</dd>
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
