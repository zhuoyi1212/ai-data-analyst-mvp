"use client";

import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { QualityIssue, QualityReport } from "@/lib/types";
import { ACTION_LABELS, ISSUE_LABELS } from "@/lib/workflow";
import { Badge, Button, Card, ErrorBanner, SectionTitle, Spinner } from "@/components/ui";
import type { PanelProps } from "./types";

type DecisionState = { action: string; fillValue?: string };

const GROUP_ORDER: Array<QualityIssue["type"]> = ["format", "duplicate", "missing", "outlier"];
const GROUP_TONE = {
  format: "blue",
  duplicate: "violet",
  missing: "amber",
  outlier: "red",
} as const;

function formatEvidence(evidence: Record<string, unknown>): string {
  return Object.entries(evidence)
    .map(([k, v]) => {
      let text: string;
      if (Array.isArray(v)) {
        text = v.slice(0, 5).map((x) => String(x)).join("、") + (v.length > 5 ? " 等" : "");
      } else if (v && typeof v === "object") {
        text = JSON.stringify(v);
      } else {
        text = String(v);
      }
      return `${k}: ${text}`;
    })
    .join("；");
}

export function QualityPanel({ sessionId, onAdvance, onJump, onError, readOnly }: PanelProps) {
  const [report, setReport] = useState<QualityReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [applying, setApplying] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [choices, setChoices] = useState<Record<string, DecisionState>>({});

  useEffect(() => {
    api
      .getQuality(sessionId)
      .then((r) => initReport(r.report))
      .catch((e: ApiError) => {
        if (e.status !== 404) onError(e.message);
      })
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  function initReport(r: QualityReport) {
    setReport(r);
    const init: Record<string, DecisionState> = {};
    for (const issue of r.issues) {
      init[issue.issue_id] = { action: issue.suggested_action };
    }
    // 已处理过：回填已有决策
    for (const [id, d] of Object.entries(r.decisions)) {
      init[id] = { action: d.action, fillValue: String(d.params?.fill_value ?? "") };
    }
    setChoices(init);
  }

  const grouped = useMemo(() => {
    if (!report) return [];
    return GROUP_ORDER.map((type) => ({
      type,
      issues: report.issues.filter((i) => i.type === type),
    })).filter((g) => g.issues.length > 0);
  }, [report]);

  const decidedCount = report
    ? report.issues.filter((i) => choices[i.issue_id]?.action).length
    : 0;

  const runChecks = async () => {
    setLocalError(null);
    setRunning(true);
    try {
      initReport((await api.runQuality(sessionId)).report);
    } catch (e) {
      setLocalError(e instanceof ApiError ? e.message : "质量检测失败。");
    } finally {
      setRunning(false);
    }
  };

  const apply = async () => {
    setLocalError(null);
    setApplying(true);
    try {
      const decisions: Record<string, { action: string; params?: unknown }> = {};
      for (const [id, c] of Object.entries(choices)) {
        decisions[id] = {
          action: c.action,
          params: c.action === "fill_value" ? { fill_value: c.fillValue || "未知" } : {},
        };
      }
      const r = await api.applyQuality(sessionId, decisions);
      setReport(r.report);
      await onAdvance();
    } catch (e) {
      setLocalError(e instanceof ApiError ? e.message : "质量处理失败。");
    } finally {
      setApplying(false);
    }
  };

  if (loading) return <CenterSpinner />;

  if (!report) {
    return (
      <Card>
        <SectionTitle
          title="数据质量检测（Data Quality）"
          desc="确定性规则检测四类问题：格式异常、重复数据、缺失值、离群值。每条问题都需要你确认处理方式后才会生成可计算的数据快照。"
        />
        {localError && <div className="mb-4"><ErrorBanner message={localError} onRetry={runChecks} /></div>}
        <Button loading={running} onClick={runChecks}>开始质量检测</Button>
      </Card>
    );
  }

  const snapshotReady = report.complete && report.snapshot_rows != null;

  return (
    <div className="space-y-4">
      {localError && <ErrorBanner message={localError} />}

      {report.issues.length === 0 && (
        <Card>
          <SectionTitle title="数据质量检测" desc="未发现需要处理的问题，可直接进入下一步。" />
          {!readOnly && <Button onClick={() => onJump(3)}>下一步：选择分析问题 →</Button>}
        </Card>
      )}

      {grouped.map((group) => (
        <Card key={group.type}>
          <div className="mb-3 flex items-center gap-2">
            <Badge tone={GROUP_TONE[group.type]}>{ISSUE_LABELS[group.type]}</Badge>
            <span className="text-sm text-zinc-500">{group.issues.length} 项</span>
          </div>
          <div className="space-y-4">
            {group.issues.map((issue) => (
              <IssueRow
                key={issue.issue_id}
                issue={issue}
                choice={choices[issue.issue_id]}
                disabled={snapshotReady || readOnly}
                onChange={(c) => setChoices((s) => ({ ...s, [issue.issue_id]: c }))}
              />
            ))}
          </div>
        </Card>
      ))}

      {snapshotReady ? (
        <Card className="border-emerald-200 bg-emerald-50/50">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-sm font-semibold text-emerald-800">
                ✓ 数据快照已生成：{report.snapshot_rows?.toLocaleString()} 行
              </p>
              <p className="mt-1 text-xs text-emerald-700">
                后续所有计算都在该快照上进行，快照哈希 sha256:
                <span className="ml-1 font-mono">{report.snapshot_hash?.slice(0, 16)}…</span>
              </p>
            </div>
            {!readOnly && <Button onClick={() => onJump(3)}>下一步：选择分析问题 →</Button>}
          </div>
        </Card>
      ) : (
        report.issues.length > 0 && (
          <Card>
            <div className="flex flex-wrap items-center gap-3">
              <Button
                loading={applying}
                disabled={decidedCount !== report.issues.length}
                onClick={apply}
              >
                应用决策并生成数据快照
              </Button>
              <span className="text-xs text-zinc-400">
                已确认 {decidedCount}/{report.issues.length} 项；默认选项为系统建议动作
              </span>
            </div>
          </Card>
        )
      )}
    </div>
  );
}

function IssueRow({
  issue,
  choice,
  disabled,
  onChange,
}: {
  issue: QualityIssue;
  choice?: DecisionState;
  disabled: boolean;
  onChange: (c: DecisionState) => void;
}) {
  return (
    <div className="rounded-lg border border-zinc-200 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="text-sm font-medium text-zinc-800">{issue.title}</p>
          <p className="mt-1 text-xs text-zinc-500">
            <span className="font-mono text-zinc-400">{issue.issue_id}</span>
          </p>
          {Object.keys(issue.evidence).length > 0 && (
            <p className="mt-1 max-w-2xl text-xs text-zinc-500">
              证据：{formatEvidence(issue.evidence)}
            </p>
          )}
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {issue.available_actions.map((action) => (
          <label
            key={action}
            className={`flex cursor-pointer items-center gap-1 rounded-full border px-3 py-1 text-xs ${
              choice?.action === action
                ? "border-accent bg-accent-soft text-accent"
                : "border-zinc-200 text-zinc-600"
            } ${disabled ? "cursor-default opacity-70" : "hover:bg-zinc-50"}`}
          >
            <input
              type="radio"
              name={issue.issue_id}
              className="sr-only"
              checked={choice?.action === action}
              disabled={disabled}
              onChange={() => onChange({ ...(choice ?? { action }), action })}
            />
            {ACTION_LABELS[action] ?? action}
          </label>
        ))}
        {choice?.action === "fill_value" && !disabled && (
          <input
            value={choice.fillValue ?? ""}
            onChange={(e) => onChange({ ...choice, fillValue: e.target.value })}
            placeholder="填充值，如：未知 / 0"
            className="rounded-md border border-zinc-300 px-2 py-1 text-xs"
          />
        )}
      </div>
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
