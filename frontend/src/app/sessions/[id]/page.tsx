"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import type { Preview, SessionMeta } from "@/lib/types";
import { stepIndexFromBackendStage, WORKFLOW_STEPS } from "@/lib/workflow";
import { Stepper } from "@/components/Stepper";
import { Card, ErrorBanner, Spinner } from "@/components/ui";
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
      <main className="mx-auto max-w-3xl px-6 py-20">
        <ErrorBanner message={fatal} />
        <div className="mt-4">
          <Link href="/" className="text-sm text-accent hover:underline">
            ← 返回上传新数据
          </Link>
        </div>
      </main>
    );
  }

  if (!meta) {
    return (
      <main className="flex min-h-screen items-center justify-center text-zinc-400">
        <Spinner />
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
    <main className="mx-auto max-w-6xl px-6 py-6">
      <header className="mb-5 flex items-center justify-between">
        <Link href="/" className="text-sm font-semibold text-zinc-900 hover:text-accent">
          AI Data Analyst
        </Link>
        <div className="text-xs text-zinc-400">
          {meta?.filename} · {meta?.shape.rows.toLocaleString()} 行 × {meta?.shape.cols} 列
        </div>
      </header>

      <Card className="mb-5 overflow-x-auto py-4">
        <Stepper current={view} reached={reached} onJump={setView} />
      </Card>

      {error && (
        <div className="mb-4">
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
        <details className="mt-4 rounded-xl border border-zinc-200 bg-white px-5 py-3 text-sm">
          <summary className="cursor-pointer text-zinc-500">查看原始数据前 10 行</summary>
          <div className="mt-3">
            <DataPreviewTable preview={preview} />
          </div>
        </details>
      )}

      <footer className="mt-8 text-center text-xs text-zinc-400">
        当前阶段：{meta ? WORKFLOW_STEPS[view]?.label : "…"} ·
        所有数值均来自确定性计算引擎，LLM 不参与计算
      </footer>
    </main>
  );
}
