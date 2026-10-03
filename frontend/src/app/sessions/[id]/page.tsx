"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import type { Preview, SessionMeta } from "@/lib/types";
import { stepIndexFromBackendStage } from "@/lib/workflow";
import { Stepper } from "@/components/Stepper";
import { ErrorBanner, Spinner } from "@/components/ui";
import { DataPreviewCard, DataPreviewTable } from "@/components/DataPreview";
import { ProfilePanel } from "@/components/panels/ProfilePanel";
import { QualityPanel } from "@/components/panels/QualityPanel";
import { QuestionsPanel } from "@/components/panels/QuestionsPanel";
import { PlanPanel } from "@/components/panels/PlanPanel";
import { RunPanel } from "@/components/panels/RunPanel";
import { InsightPanel } from "@/components/panels/InsightPanel";

export default function SessionPage() {
  const params = useParams<{ id: string }>();
  const sessionId = params.id;

  const [meta, setMeta] = useState<SessionMeta | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState(0);
  const [reached, setReached] = useState(0);

  const refreshMeta = useCallback(async () => {
    const { meta: m } = await api.getSession(sessionId);
    setMeta(m);
    const idx = Math.max(1, stepIndexFromBackendStage(m.stage));
    setReached((r) => Math.max(r, idx));
    return m;
  }, [sessionId]);

  useEffect(() => {
    (async () => {
      try {
        const m = await refreshMeta();
        setView(Math.max(1, stepIndexFromBackendStage(m.stage)));
        const p = await api.getPreview(sessionId);
        setPreview(p);
      } catch (e) {
        setFatal(e instanceof ApiError ? e.message : "会话加载失败。");
      }
    })();
  }, [refreshMeta, sessionId]);

  const advance = useCallback(async () => {
    setError(null);
    try {
      const m = await refreshMeta();
      setView(stepIndexFromBackendStage(m.stage));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "状态刷新失败。");
    }
  }, [refreshMeta]);

  if (fatal) {
    return (
      <main className="mx-auto max-w-3xl px-6 py-28">
        <ErrorBanner message={fatal} />
        <div className="mt-5">
          <Link href="/" className="link-apple text-sm">
            ← 返回上传新数据
          </Link>
        </div>
      </main>
    );
  }

  if (!meta) {
    return (
      <main className="flex min-h-screen items-center justify-center text-muted">
        <Spinner className="h-7 w-7 text-apple" />
      </main>
    );
  }

  const panelProps = {
    sessionId,
    onAdvance: advance,
    onJump: (i: number) => setView(i),
    onError: setError,
    readOnly: view < reached,
  };

  return (
    <div className="min-h-screen bg-white">
      {/* ------------------------------------------------ 顶部导航 */}
      <header className="glass-bar sticky top-0 z-40">
        <div className="mx-auto flex h-12 max-w-6xl items-center justify-between px-6">
          <Link
            href="/"
            className="text-sm font-semibold tracking-tight text-ink transition-colors hover:text-apple"
          >
            AI Data Analyst
          </Link>
          <div className="flex items-center gap-5 text-xs text-muted">
            <Link
              href={`/sessions/${sessionId}/dashboard`}
              className="font-medium text-apple hover:text-apple-hover"
            >
              自动分析工作台 →
            </Link>
            <span className="hidden sm:inline">
              {meta.filename} · {meta.shape.rows.toLocaleString()} 行 ×{" "}
              {meta.shape.cols} 列
            </span>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-6 py-8">
        <div className="animate-fade-up">
          <Stepper current={view} reached={reached} onJump={setView} />
        </div>

        <div className="mt-6 animate-fade-up" style={{ animationDelay: "80ms" }}>
          {error && (
            <div className="mb-5">
              <ErrorBanner message={error} onRetry={() => setError(null)} />
            </div>
          )}

          {view === 1 && <ProfilePanel {...panelProps} />}
          {view === 2 && <QualityPanel {...panelProps} />}
          {view === 3 && <QuestionsPanel {...panelProps} />}
          {view === 4 && <PlanPanel {...panelProps} />}
          {view === 5 && <RunPanel {...panelProps} />}
          {view === 6 && <InsightPanel {...panelProps} />}

          {view === 0 && preview && <DataPreviewCard preview={preview} />}
          {view >= 1 && preview && (
            <details className="group mt-5 rounded-3xl bg-canvas px-6 py-4 text-sm">
              <summary className="flex cursor-pointer list-none items-center justify-between text-muted transition-colors hover:text-ink">
                <span className="font-medium">查看原始数据前 10 行</span>
                <svg
                  className="h-4 w-4 transition-transform duration-300 group-open:rotate-180"
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
              <div className="mt-4">
                <DataPreviewTable preview={preview} />
              </div>
            </details>
          )}
        </div>

        <footer className="mt-12 border-t border-hairline pt-6 text-center text-xs text-faint">
          所有数值均来自确定性计算引擎，LLM 不参与计算。
        </footer>
      </main>
    </div>
  );
}
