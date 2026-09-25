"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { RecommendedQuestion } from "@/lib/types";
import { CATEGORY_LABELS, OP_LABELS } from "@/lib/workflow";
import { Badge, Button, Card, EmptyState, ErrorBanner, SectionTitle, Spinner } from "@/components/ui";
import type { PanelProps } from "./types";

const CATEGORY_TONE: Record<string, "blue" | "violet" | "green" | "amber" | "red" | "neutral"> = {
  overview: "blue",
  trend: "violet",
  comparison: "green",
  share: "amber",
  ranking: "neutral",
  anomaly: "red",
  correlation: "violet",
};

export function QuestionsPanel({ sessionId, onAdvance, onJump, onError, readOnly }: PanelProps) {
  const [questions, setQuestions] = useState<RecommendedQuestion[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [custom, setCustom] = useState("");
  const [active, setActive] = useState<number | null>(null);

  useEffect(() => {
    api
      .getQuestions(sessionId)
      .then((r) => setQuestions(r.questions))
      .catch((e: ApiError) => {
        if (e.status !== 404) onError(e.message);
      })
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  const makePlan = async (question: string, index?: number) => {
    setLocalError(null);
    setBusy(true);
    try {
      await api.generatePlan(sessionId, question, index);
      await onAdvance();
      onJump(4);
    } catch (e) {
      setLocalError(e instanceof ApiError ? e.message : "方案生成失败。");
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <CenterSpinner />;

  if (!questions) {
    return (
      <Card>
        <SectionTitle
          title="智能问题推荐（Question Recommendation）"
          desc="基于已确认的数据字典、字段关系与数据特征生成问题；每条推荐都会说明推荐依据，你也可以直接输入自定义问题。"
        />
        {localError && <div className="mb-4"><ErrorBanner message={localError} /></div>}
        <Button loading={busy} onClick={() => void generate()}>
          生成推荐问题
        </Button>
      </Card>
    );
  }

  async function generate() {
    setLocalError(null);
    setBusy(true);
    try {
      setQuestions((await api.generateQuestions(sessionId)).questions);
    } catch (e) {
      setLocalError(e instanceof ApiError ? e.message : "推荐生成失败。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <SectionTitle
          title="选择你想分析的问题"
          desc="问题按分析类型分组；点击「生成分析方案」进入由有限算子组成的结构化方案。"
        />
        {localError && <div className="mb-4"><ErrorBanner message={localError} /></div>}

        {questions.length === 0 ? (
          <EmptyState title="暂无推荐问题" desc="可重新生成或直接输入自定义问题" />
        ) : (
          <div className="grid gap-3 md:grid-cols-2">
            {questions.map((q, i) => (
              <div
                key={i}
                className={`flex flex-col gap-2 rounded-xl border p-4 transition-colors ${
                  active === i ? "border-accent bg-accent-soft/50" : "border-zinc-200 bg-white"
                }`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={CATEGORY_TONE[q.category] ?? "neutral"}>
                    {CATEGORY_LABELS[q.category] ?? q.category}
                  </Badge>
                  <Badge>{OP_LABELS[q.target_op] ?? q.target_op}</Badge>
                </div>
                <p className="text-sm font-medium text-zinc-900">{q.text}</p>
                <p className="text-xs leading-relaxed text-zinc-500">
                  <span className="text-zinc-400">推荐依据：</span>
                  {q.rationale}
                </p>
                <div className="flex flex-wrap gap-1">
                  {q.fields.map((f) => (
                    <span key={f} className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-[11px] text-zinc-500">
                      {f}
                    </span>
                  ))}
                </div>
                {!readOnly && (
                  <div className="mt-1">
                    <Button
                      variant="secondary"
                      className="px-3 py-1.5 text-xs"
                      loading={busy && active === i}
                      onClick={() => {
                        setActive(i);
                        void makePlan(q.text, i);
                      }}
                    >
                      生成分析方案 →
                    </Button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

      {!readOnly && (
        <Card>
          <SectionTitle title="没有想问的？直接输入自定义问题" desc="规划器同样只会使用 10 个受支持的分析算子，不支持的分析会明确提示。" />
          <div className="flex flex-wrap gap-2">
            <input
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              placeholder="例如：各品类本月销售额相比上月如何变化？"
              className="min-w-[260px] flex-1 rounded-md border border-zinc-300 px-3 py-2 text-sm"
            />
            <Button
              loading={busy && active === -1}
              disabled={custom.trim().length < 4}
              onClick={() => {
                setActive(-1);
                void makePlan(custom.trim(), undefined);
              }}
            >
              用该问题生成方案
            </Button>
          </div>
        </Card>
      )}
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
