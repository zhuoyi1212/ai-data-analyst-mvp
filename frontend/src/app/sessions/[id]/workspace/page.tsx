"use client";

// Workspace：自动分析产物的统一入口——
// Dashboard（12-column）与深度分析报告双 Tab，证据可双向跳转。

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import type {
  AnalysisReportArtifact,
  DashboardArtifact,
} from "@/lib/types";
import { ErrorBanner, Spinner } from "@/components/ui";
import { WorkspaceDashboard } from "@/components/workspace/WorkspaceDashboard";
import { WorkspaceReport } from "@/components/workspace/WorkspaceReport";

type Tab = "dashboard" | "report";

export default function WorkspacePage() {
  const params = useParams<{ id: string }>();
  const sessionId = params.id;

  const [tab, setTab] = useState<Tab>("dashboard");
  const [dashboard, setDashboard] = useState<DashboardArtifact | null>(null);
  const [report, setReport] = useState<AnalysisReportArtifact | null>(null);
  const [highlightedId, setHighlightedId] = useState<string | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const [d, r] = await Promise.all([
          api.getDashboard(sessionId),
          api.getReport(sessionId),
        ]);
        setDashboard(d);
        setReport(r);
      } catch (e) {
        setFatal(e instanceof ApiError ? e.message : "产物加载失败。");
      }
    })();
  }, [sessionId]);

  const locateView = useCallback((viewId: string) => {
    setHighlightedId(viewId);
    setTab("dashboard");
  }, []);

  if (fatal) {
    return (
      <main className="mx-auto max-w-3xl px-6 py-28">
        <ErrorBanner message={fatal} />
        <div className="mt-5 flex gap-4">
          <Link
            href={`/sessions/${sessionId}/analyzing`}
            className="link-apple text-sm"
          >
            ← 返回分析进度
          </Link>
          <Link href="/" className="link-apple text-sm">
            上传新数据
          </Link>
        </div>
      </main>
    );
  }

  if (!dashboard || !report) {
    return (
      <main className="flex min-h-screen items-center justify-center text-muted">
        <Spinner className="h-7 w-7 text-apple" />
      </main>
    );
  }

  return (
    <div className="min-h-screen bg-canvas">
      <header className="glass-bar sticky top-0 z-40">
        <div className="mx-auto flex h-12 max-w-7xl items-center justify-between px-6">
          <Link
            href="/"
            className="text-sm font-semibold tracking-tight text-ink transition-colors hover:text-apple"
          >
            AI Data Analyst
          </Link>
          <div className="flex items-center gap-5 text-xs">
            <nav className="flex items-center gap-1 rounded-full bg-white p-1 shadow-card">
              <TabButton
                active={tab === "dashboard"}
                onClick={() => setTab("dashboard")}
              >
                Dashboard
              </TabButton>
              <TabButton
                active={tab === "report"}
                onClick={() => setTab("report")}
              >
                深度报告
              </TabButton>
            </nav>
            <Link
              href={`/sessions/${sessionId}`}
              className="hidden font-medium text-muted transition-colors hover:text-ink sm:inline"
            >
              高级模式
            </Link>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-6 py-8">
        <div className="mb-6">
          <h1 className="text-2xl font-semibold tracking-[-0.02em] text-ink">
            {dashboard.title}
          </h1>
          <p className="mt-1 text-xs text-muted">
            基于 {dashboard.scope.snapshot_rows.toLocaleString()} 行数据 ·
            当前范围 {dashboard.scope.participating_rows.toLocaleString()} 行
          </p>
        </div>

        {tab === "dashboard" ? (
          <WorkspaceDashboard
            sessionId={sessionId}
            dashboard={dashboard}
            onReloaded={setDashboard}
            highlightedId={highlightedId}
            onConsumeHighlight={() => setHighlightedId(null)}
          />
        ) : (
          <WorkspaceReport report={report} onLocateView={locateView} />
        )}
      </main>
    </div>
  );
}

function TabButton({
  active, onClick, children,
}: {
  active: boolean; onClick: () => void; children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-full px-4 py-1.5 text-xs font-medium transition-all
        ${active ? "bg-apple text-white shadow-sm" : "text-muted hover:text-ink"}`}
    >
      {children}
    </button>
  );
}
