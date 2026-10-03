"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type {
  FollowUpQuestion,
  GroundedNumber,
  Insight,
  Ledger,
} from "@/lib/types";
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

const CONF_TONE = {
  high: "green",
  medium: "amber",
  low: "red",
} as const;
const CONF_LABEL = {
  high: "高可信",
  medium: "中可信",
  low: "需谨慎",
} as const;

export function InsightPanel({
  sessionId,
  onAdvance,
  onJump,
  onError,
  readOnly,
}: PanelProps) {
  const [insights, setInsights] = useState<Insight[] | null>(null);
  const [followups, setFollowups] = useState<FollowUpQuestion[] | null>(null);
  const [ledger, setLedger] = useState<Ledger | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [evidenceFor, setEvidenceFor] = useState<Insight | null>(null);

  useEffect(() => {
    Promise.all([
      api
        .getInsights(sessionId)
        .catch((e: ApiError) =>
          e.status === 404 ? null : Promise.reject(e),
        ),
      api
        .getFollowups(sessionId)
        .catch((e: ApiError) =>
          e.status === 404 ? null : Promise.reject(e),
        ),
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
      setLocalError(
        e instanceof ApiError ? e.message : "洞察生成失败。",
      );
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
      setLocalError(
        e instanceof ApiError ? e.message : "追问生成失败。",
      );
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
      setLocalError(
        e instanceof ApiError ? e.message : "无法为该追问生成方案。",
      );
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <CenterSpinner />;

  return (
    <div className="space-y-5">
      {localError && <ErrorBanner message={localError} />}

      {!insights ? (
        <Card>
          <SectionTitle
            title="业务洞察（Insight）"
            desc="洞察只基于引擎结果摘要撰写，其中每个数字都必须指向具体计算结果；因果性表述被严格禁止，相关分析会强制标注免责声明。"
          />
          {!readOnly && (
            <Button loading={busy} onClick={generateInsights}>
              生成业务洞察
            </Button>
          )}
        </Card>
      ) : (
        <>
          <div className="space-y-4">
            {insights.map((ins, i) => (
              <Card key={i}>
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-2.5">
                    <span className="flex h-7 w-7 items-center justify-center rounded-full bg-apple-soft text-xs font-semibold text-apple">
                      {i + 1}
                    </span>
                    <Badge tone={CONF_TONE[ins.confidence]}>
                      {CONF_LABEL[ins.confidence]}
                    </Badge>
                    {ins.needs_further_validation && (
                      <Badge tone="amber">建议进一步验证</Badge>
                    )}
                  </div>
                  {ledger && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setEvidenceFor(ins)}
                    >
                      查看完整证据 ↗
                    </Button>
                  )}
                </div>
                <p className="mt-4 text-sm leading-7 text-ink">
                  {ins.text}
                </p>
                {ins.disclaimer && (
                  <p className="mt-3 rounded-2xl bg-canvas px-4 py-3 text-xs leading-relaxed text-muted">
                    免责声明：{ins.disclaimer}
                  </p>
                )}
                <div className="mt-4 flex flex-wrap items-center gap-2">
                  <span className="text-xs text-faint">接地数字：</span>
                  {ins.numbers.map((n, j) => (
                    <span
                      key={j}
                      className="rounded-full bg-apple-soft px-2.5 py-1 text-xs font-medium text-apple"
                    >
                      {n.label}: {formatValue(n.value)}
                    </span>
                  ))}
                </div>
                <p className="mt-3 text-xs text-faint">
                  可信度依据：{ins.confidence_reason}
                </p>
              </Card>
            ))}
          </div>

          <Card>
            <SectionTitle
              title="后续可以继续问（Follow-up）"
              desc="追问同样带推荐依据；点击任一问题会立即生成新的结构化方案，形成分析闭环。"
            />
            {!followups ? (
              !readOnly && (
                <Button loading={busy} onClick={generateFollowups}>
                  生成后续问题建议
                </Button>
              )
            ) : (
              <div className="grid gap-3">
                {followups.map((q, i) => (
                  <button
                    key={i}
                    type="button"
                    disabled={busy}
                    onClick={() => void askFollowup(q)}
                    className="group flex items-start justify-between gap-3 rounded-3xl border border-hairline bg-canvas/40 px-5 py-4 text-left transition-all duration-300 hover:-translate-y-0.5 hover:border-apple/40 hover:bg-apple-tint hover:shadow-card disabled:opacity-50 active:translate-y-0"
                  >
                    <span>
                      <span className="block text-sm font-medium text-ink">
                        {q.text}
                      </span>
                      <span className="mt-1.5 block text-xs text-faint">
                        依据：{q.rationale}
                      </span>
                      <span className="mt-2 flex flex-wrap gap-1.5">
                        {q.fields.map((f) => (
                          <span
                            key={f}
                            className="rounded-md bg-white px-2 py-0.5 font-mono text-[11px] text-muted ring-1 ring-hairline"
                          >
                            {f}
                          </span>
                        ))}
                      </span>
                    </span>
                    <span className="shrink-0 text-xs font-medium text-apple opacity-0 transition-opacity group-hover:opacity-100">
                      分析 →
                    </span>
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
    <div
      className="fixed inset-0 z-[60] flex justify-end bg-ink/25 backdrop-blur-sm"
      onClick={onClose}
    >
      <aside
        className="h-full w-full max-w-xl animate-slide-in-right overflow-y-auto bg-white/95 p-7 shadow-pop backdrop-blur-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-6 flex items-start justify-between gap-4">
          <h3 className="text-xl font-semibold tracking-tight text-ink">
            洞察完整证据链
          </h3>
          <button
            onClick={onClose}
            className="flex h-8 w-8 items-center justify-center rounded-full bg-canvas text-muted transition-colors hover:text-ink"
          >
            ✕
          </button>
        </div>

        <section className="mb-6">
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-faint">
            洞察结论
          </p>
          <p className="text-sm leading-7 text-ink">{insight.text}</p>
          <p className="mt-3 text-xs text-faint">
            可信度：{CONF_LABEL[insight.confidence]} ·{" "}
            {insight.confidence_reason}
          </p>
        </section>

        <section className="mb-6">
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-faint">
            数据范围与快照
          </p>
          <dl className="space-y-1.5 rounded-2xl bg-canvas p-4 text-xs text-muted">
            <Meta k="分析问题" v={ledger.question} />
            <Meta k="数据范围" v={ledger.data_scope || "（未额外筛选）"} />
            <Meta k="快照行数" v={ledger.snapshot_rows.toLocaleString()} />
            <Meta
              k="参与计算行数"
              v={ledger.participating_rows.toLocaleString()}
            />
            <Meta
              k="快照哈希"
              v={
                <span className="font-mono">
                  {ledger.snapshot_hash.slice(0, 24)}…
                </span>
              }
            />
            <Meta
              k="方案哈希"
              v={<span className="font-mono">{ledger.plan_hash}</span>}
            />
          </dl>
        </section>

        <section className="mb-6">
          <p className="mb-3 text-xs font-medium uppercase tracking-wide text-faint">
            接地数字（{insight.numbers.length}）→ 对应计算步骤
          </p>
          <div className="space-y-3">
            {insight.numbers.map((n, i) => (
              <NumberEvidence key={i} n={n} ledger={ledger} />
            ))}
          </div>
        </section>

        <section>
          <p className="mb-3 text-xs font-medium uppercase tracking-wide text-faint">
            完整计算台账
          </p>
          <div className="space-y-2.5">
            {ledger.steps.map((s, i) => (
              <div
                key={s.step_id}
                className="rounded-2xl border border-hairline bg-canvas/40 p-4 text-xs"
              >
                <div className="flex items-center gap-2">
                  <span className="flex h-4 w-4 items-center justify-center rounded-full bg-ink text-[9px] font-semibold text-white">
                    {i + 1}
                  </span>
                  <Badge tone="blue">
                    {OP_LABELS[s.op] ?? s.op}
                  </Badge>
                  <span className="font-mono text-faint">
                    {s.step_id}
                  </span>
                </div>
                <p className="mt-2 text-ink/80">{s.description}</p>
                <p className="mt-1 font-mono text-muted">{s.formula}</p>
                <p className="mt-1.5 text-faint">
                  参数：{JSON.stringify(s.params)}；{s.input_rows}→
                  {s.output_rows} 行
                </p>
              </div>
            ))}
          </div>
        </section>
      </aside>
    </div>
  );
}

function NumberEvidence({
  n,
  ledger,
}: {
  n: GroundedNumber;
  ledger: Ledger;
}) {
  const step = ledger.steps.find((s) => s.step_id === n.ref_step);
  return (
    <div className="rounded-2xl border border-hairline p-4">
      <div className="flex flex-wrap items-center gap-2.5 text-sm">
        <span className="font-semibold text-ink">{n.label}</span>
        <span className="font-medium tabular-nums text-apple">
          {formatValue(n.value)}
        </span>
      </div>
      <dl className="mt-2.5 space-y-1.5 text-xs text-muted">
        <Meta
          k="引用类型"
          v={
            n.ref_kind === "metric"
              ? "指标结果"
              : n.ref_kind === "row"
                ? "结果行"
                : "汇总指标"
          }
        />
        <Meta
          k="定位键"
          v={
            <span className="font-mono">
              {JSON.stringify(n.ref_keys) || "{}"}
            </span>
          }
        />
        {step && <Meta k="步骤口径" v={step.description} />}
        {step && (
          <Meta
            k="计算公式"
            v={<span className="font-mono">{step.formula}</span>}
          />
        )}
        {step && (
          <Meta
            k="筛选条件/参数"
            v={<span className="font-mono">{JSON.stringify(step.params)}</span>}
          />
        )}
        {step && (
          <Meta
            k="输入行数"
            v={`${step.input_rows.toLocaleString()} 行`}
          />
        )}
      </dl>
    </div>
  );
}

function Meta({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex gap-2.5">
      <dt className="w-24 shrink-0 text-faint">{k}</dt>
      <dd className="min-w-0 flex-1 break-all">{v}</dd>
    </div>
  );
}

function CenterSpinner() {
  return (
    <div className="flex items-center justify-center py-24 text-faint">
      <Spinner className="h-6 w-6 text-apple" />
    </div>
  );
}
