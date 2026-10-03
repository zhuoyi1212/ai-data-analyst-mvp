"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { RecommendedQuestion } from "@/lib/types";
import { CATEGORY_LABELS, OP_LABELS } from "@/lib/workflow";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorBanner,
  SectionTitle,
  Spinner,
} from "@/components/ui";
import type { PanelProps } from "./types";

const CATEGORY_TONE: Record<
  string,
  "blue" | "violet" | "green" | "amber" | "red" | "neutral"
> = {
  overview: "blue",
  trend: "violet",
  comparison: "green",
  share: "amber",
  ranking: "neutral",
  anomaly: "red",
  correlation: "violet",
};

export function QuestionsPanel({
  sessionId,
  onAdvance,
  onJump,
  onError,
  readOnly,
}: PanelProps) {
  const [questions, setQuestions] = useState<RecommendedQuestion[] | null>(
    null,
  );
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
        {localError && (
          <div className="mb-5">
            <ErrorBanner message={localError} />
          </div>
        )}
        <Button loading={busy} onClick={() => void generate()}>
          生成推荐问题
        </Button>
      </Card>
    );
  }

  return (
    <div className="space-y-5">
      <Card>
        <SectionTitle
          title="选择你想分析的问题"
          desc="点击「生成分析方案」，进入由有限算子组成的结构化方案。"
        />
        {localError && (
          <div className="mb-5">
            <ErrorBanner message={localError} />
          </div>
        )}

        {questions.length === 0 ? (
          <EmptyState
            title="暂无推荐问题"
            desc="可重新生成，或直接在下方输入自定义问题。"
          />
        ) : (
          <div className="grid gap-4 md:grid-cols-2">
            {questions.map((q, i) => (
              <div
                key={i}
                className={`flex flex-col rounded-3xl border p-6 transition-all duration-300
                  ${active === i
                    ? "border-apple/50 bg-apple-tint shadow-card"
                    : "border-hairline bg-canvas/40 hover:border-line hover:bg-white hover:shadow-card"
                  }`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={CATEGORY_TONE[q.category] ?? "neutral"}>
                    {CATEGORY_LABELS[q.category] ?? q.category}
                  </Badge>
                  <Badge>{OP_LABELS[q.target_op] ?? q.target_op}</Badge>
                </div>
                <p className="mt-3 text-sm font-medium leading-relaxed text-ink">
                  {q.text}
                </p>
                <p className="mt-2 text-xs leading-relaxed text-muted">
                  <span className="text-faint">推荐依据：</span>
                  {q.rationale}
                </p>
                <div className="mt-3 flex flex-wrap gap-1.5">
                  {q.fields.map((f) => (
                    <span
                      key={f}
                      className="rounded-md bg-white px-2 py-0.5 font-mono text-[11px] text-muted ring-1 ring-hairline"
                    >
                      {f}
                    </span>
                  ))}
                </div>
                {!readOnly && (
                  <div className="mt-5">
                    <Button
                      variant="secondary"
                      size="sm"
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
          <SectionTitle
            title="没有想问的？直接输入自定义问题"
            desc="规划器同样只会使用受支持的分析算子，不支持的分析会明确提示。"
          />
          <div className="flex flex-wrap items-center gap-3">
            <input
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              placeholder="例如：各品类本月销售额相比上月如何变化？"
              className="field-input min-w-[260px] flex-1"
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
    <div className="flex items-center justify-center py-24 text-muted">
      <Spinner className="h-6 w-6 text-apple" />
    </div>
  );
}
