"use client";

// 自动分析进度页：上传后无任何逐步确认，AI 自动完成
// 理解 → 扫描 → 信号 → 深挖 → 合成；仅在关键歧义/严重错误时就地提问。

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import type { AutoAnalysisState, AutoStageName } from "@/lib/types";
import { Button, ErrorBanner, Spinner } from "@/components/ui";

const STAGE_LABELS: Record<AutoStageName, string> = {
  profile: "理解数据语义",
  quality: "数据质量体检",
  scan: "多维广度扫描",
  signals: "信号检测与排序",
  diagnostic: "关键问题深挖",
  synthesis: "Dashboard 与报告合成",
};

const STAGE_ORDER: AutoStageName[] = [
  "profile", "quality", "scan", "signals", "diagnostic", "synthesis",
];

export default function AnalyzingPage() {
  const params = useParams<{ id: string }>();
  const sessionId = params.id;
  const router = useRouter();

  const [state, setState] = useState<AutoAnalysisState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [answersDraft, setAnswersDraft] = useState<Record<string, string>>({});
  const [retryNonce, setRetryNonce] = useState(0);
  const startedRef = useRef(false);

  const poll = useCallback(async () => {
    const s = await api.getAutoState(sessionId);
    setState(s);
    return s;
  }, [sessionId]);

  // 启动自动分析（首次一次；failed 后点击重试经 retryNonce 重新触发）
  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;
    (async () => {
      try {
        const s = await api.startAuto(sessionId);
        setState(s);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "自动分析启动失败。");
      }
    })();
  }, [sessionId, retryNonce]);

  // 轮询（completed/failed/等待人工输入时暂停，避免无意义请求）
  useEffect(() => {
    if (
      state?.status === "completed" ||
      state?.status === "failed" ||
      state?.status === "waiting_input"
    ) {
      return;
    }
    const t = setInterval(() => {
      poll().catch((e: ApiError) => setError(e.message));
    }, 1500);
    return () => clearInterval(t);
  }, [poll, state?.status]);

  // 完成 → Workspace
  useEffect(() => {
    if (state?.status === "completed") {
      const t = setTimeout(
        () => router.replace(`/sessions/${sessionId}/workspace`),
        600,
      );
      return () => clearTimeout(t);
    }
  }, [state?.status, sessionId, router]);

  const submitAnswers = useCallback(async () => {
    setError(null);
    try {
      const s = await api.startAuto(sessionId, answersDraft);
      setState(s);
      setAnswersDraft({});
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "提交失败，请重试。");
    }
  }, [answersDraft, sessionId]);

  const stageMap = new Map(state?.stages.map((s) => [s.stage, s]));
  const needsInput = state?.needs_input ?? [];

  return (
    <div className="min-h-screen bg-white">
      <header className="glass-bar sticky top-0 z-40">
        <div className="mx-auto flex h-12 max-w-3xl items-center justify-between px-6">
          <Link
            href="/"
            className="text-sm font-semibold tracking-tight text-ink transition-colors hover:text-apple"
          >
            AI Data Analyst
          </Link>
          <Link
            href={`/sessions/${sessionId}`}
            className="text-xs text-muted transition-colors hover:text-ink"
          >
            高级模式 / 查看分析依据
          </Link>
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-6 pb-24 pt-16">
        <div className="animate-fade-up">
          <p className="text-sm font-medium text-apple">
            {state?.status === "completed"
              ? "分析完成"
              : "AI 正在自动分析你的数据"}
          </p>
          <h1 className="mt-3 text-3xl font-semibold tracking-[-0.02em] text-ink sm:text-4xl">
            {state?.status === "completed"
              ? "Dashboard 与深度报告已就绪"
              : "理解、扫描、深挖与合成中…"}
          </h1>
          <p className="mt-3 text-sm leading-relaxed text-muted">
            无需选择问题，也无需确认方案。只有遇到关键歧义或严重数据错误时，
            AI 才会在下方询问你。
          </p>
        </div>

        {error && (
          <div className="mt-8 animate-fade-up">
            <ErrorBanner message={error} onRetry={() => setError(null)} />
          </div>
        )}

        {/* -------------------------------------------- 阶段时间线 */}
        <div className="mt-10 animate-fade-up" style={{ animationDelay: "80ms" }}>
          <ol className="relative">
            {STAGE_ORDER.map((name) => {
              const log = stageMap.get(name);
              const status = log?.status ?? "pending";
              return (
                <li key={name} className="flex gap-4 pb-7 last:pb-0">
                  <div className="flex flex-col items-center">
                    <StageDot status={status} />
                  </div>
                  <div className="flex-1 pb-1">
                    <div className="flex items-center justify-between">
                      <p
                        className={`text-sm font-medium tracking-tight ${
                          status === "pending" ? "text-faint" : "text-ink"
                        }`}
                      >
                        {STAGE_LABELS[name]}
                      </p>
                      <span className="text-xs text-faint">
                        {status === "completed" && "已完成"}
                        {status === "running" && "进行中"}
                        {status === "skipped" && "无需处理"}
                        {status === "failed" && "失败"}
                      </span>
                    </div>
                    {log?.message && (
                      <p className="mt-1 text-xs leading-relaxed text-muted">
                        {log.message}
                      </p>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
        </div>

        {/* -------------------------------------------- 最小化拦截问题 */}
        {needsInput.length > 0 && (
          <div className="mt-10 rounded-3xl border border-apple/20 bg-apple-tint p-6 sm:p-7">
            <p className="text-sm font-semibold text-ink">
              需要你确认一个关键问题
            </p>
            <div className="mt-5 space-y-6">
              {needsInput.map((q) => (
                <div key={q.question_id}>
                  <p className="text-sm leading-relaxed text-ink">{q.prompt}</p>
                  {q.options.length > 0 && (
                    <div className="mt-3 flex flex-wrap gap-2">
                      {q.options.map((opt) => {
                        const active = answersDraft[q.question_id] === opt;
                        return (
                          <button
                            key={opt}
                            type="button"
                            onClick={() =>
                              setAnswersDraft((d) => ({
                                ...d, [q.question_id]: opt,
                              }))
                            }
                            className={`rounded-full border px-4 py-1.5 text-xs font-medium transition-all
                              ${active
                                ? "border-apple bg-apple text-white"
                                : "border-line bg-white text-muted hover:text-ink"}`}
                          >
                            {opt}
                          </button>
                        );
                      })}
                    </div>
                  )}
                  {q.allow_free_text && (
                    <input
                      type="text"
                      value={answersDraft[q.question_id] ?? ""}
                      onChange={(e) =>
                        setAnswersDraft((d) => ({
                          ...d, [q.question_id]: e.target.value,
                        }))
                      }
                      placeholder="或直接输入你的答案"
                      className="mt-3 w-full rounded-2xl border border-line bg-white px-4 py-2.5 text-sm text-ink outline-none transition-colors focus:border-apple"
                    />
                  )}
                </div>
              ))}
            </div>
            <div className="mt-6 flex justify-end">
              <Button
                size="sm"
                loading={false}
                disabled={needsInput.some(
                  (q) => !answersDraft[q.question_id]?.trim(),
                )}
                onClick={() => void submitAnswers()}
              >
                提交并继续分析
              </Button>
            </div>
          </div>
        )}

        {state?.status === "completed" && (
          <div className="mt-10 flex items-center gap-3 text-sm text-muted">
            <Spinner className="h-4 w-4 text-apple" />
            正在进入 Workspace…
          </div>
        )}
        {state?.status === "failed" && (
          <div className="mt-8">
            <ErrorBanner
              title="分析未能完成"
              message={state.error ?? "未知错误"}
            />
            <div className="mt-5">
              <Button
                size="sm"
                variant="secondary"
                onClick={() => {
                  startedRef.current = false;
                  setState(null);
                  setRetryNonce((n) => n + 1);
                }}
              >
                重新尝试
              </Button>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

function StageDot({ status }: { status: string }) {
  if (status === "completed") {
    return (
      <span className="flex h-6 w-6 items-center justify-center rounded-full bg-success-soft text-success">
        <svg
          className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="none"
          stroke="currentColor" strokeWidth="2.4"
          strokeLinecap="round" strokeLinejoin="round"
        >
          <path d="M4 10.5l4 4 8-9" />
        </svg>
      </span>
    );
  }
  if (status === "running") {
    return (
      <span className="flex h-6 w-6 items-center justify-center rounded-full bg-apple-soft text-apple">
        <Spinner className="h-3.5 w-3.5" />
      </span>
    );
  }
  if (status === "skipped") {
    return (
      <span className="h-6 w-6 rounded-full border border-line bg-canvas" />
    );
  }
  if (status === "failed") {
    return <span className="h-6 w-6 rounded-full bg-danger-soft" />;
  }
  return (
    <span className="h-6 w-6 rounded-full border border-line bg-white" />
  );
}
