"use client";

// Workspace · 深度分析报告：八节排版；每条结论的证据引用可点击
// 跳回 Dashboard 高亮对应图表。所有数字由后端确定性计算并经接地校验。

import type { AnalysisReportArtifact } from "@/lib/types";
import { Badge } from "@/components/ui";

const CLAIM_BADGE: Record<string, { tone: "neutral" | "blue" | "amber" | "green" | "violet"; label: string }> = {
  fact: { tone: "neutral", label: "事实" },
  signal: { tone: "blue", label: "信号" },
  hypothesis: { tone: "amber", label: "假设" },
  conclusion: { tone: "green", label: "结论" },
};

export function WorkspaceReport({
  report, onLocateView,
}: {
  report: AnalysisReportArtifact;
  onLocateView: (viewId: string) => void;
}) {
  return (
    <div className="space-y-6">
      {report.sections.map((section) => (
        <section
          key={section.section_id}
          className="rounded-3xl border border-hairline bg-white p-6 shadow-card sm:p-8"
        >
          <h2 className="text-lg font-semibold tracking-tight text-ink">
            {section.title}
          </h2>
          <ul className="mt-5 space-y-4">
            {section.claims.map((claim) => {
              const badge = CLAIM_BADGE[claim.claim_type] ?? CLAIM_BADGE.fact;
              return (
                <li
                  key={claim.claim_id}
                  className="rounded-2xl bg-canvas/70 px-5 py-4"
                >
                  <div className="flex items-center gap-2">
                    <Badge tone={badge.tone}>{badge.label}</Badge>
                    <span className="text-xs text-faint">
                      {claim.claim_id}
                    </span>
                  </div>
                  <p className="mt-2 text-sm leading-relaxed text-ink">
                    {claim.text}
                  </p>
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    {claim.evidence_view_ids.map((vid) => (
                      <button
                        key={vid}
                        type="button"
                        onClick={() => onLocateView(vid)}
                        className="rounded-full border border-line bg-white px-3 py-0.5 text-xs font-medium text-apple transition-all hover:border-apple"
                      >
                        证据：{vid}
                      </button>
                    ))}
                  </div>
                  {claim.limitations && (
                    <p className="mt-3 text-xs leading-relaxed text-faint">
                      局限：{claim.limitations}
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}
