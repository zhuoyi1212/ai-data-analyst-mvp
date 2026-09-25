"use client";

import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { DataDictionary, FieldProfile, SemanticType } from "@/lib/types";
import { SEMANTIC_LABELS } from "@/lib/workflow";
import { Badge, Button, Card, ErrorBanner, SectionTitle, Spinner } from "@/components/ui";
import type { PanelProps } from "./types";

const SEMANTIC_OPTIONS: SemanticType[] = [
  "metric",
  "dimension",
  "date",
  "id",
  "geo",
  "unknown",
];

const CONF_TONE = {
  high: "green",
  medium: "amber",
  low: "red",
} as const;

export function ProfilePanel({ sessionId, onAdvance, onJump, onError, readOnly }: PanelProps) {
  const [dictionary, setDictionary] = useState<DataDictionary | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  // 用户对未确认字段的决策：name → {semantic_type, meaning, ignored}
  const [decisions, setDecisions] = useState<Record<string, {
    semantic_type: SemanticType;
    meaning: string;
    ignored: boolean;
  }>>({});

  useEffect(() => {
    api
      .getProfile(sessionId)
      .then((r) => initFromDictionary(r.dictionary))
      .catch((e: ApiError) => {
        if (e.status !== 404) onError(e.message);
      })
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  function initFromDictionary(d: DataDictionary) {
    setDictionary(d);
    const pending: typeof decisions = {};
    for (const f of d.fields) {
      if (!f.confirmed_by_user && !(f.confidence === "high" && f.semantic_type !== "unknown")) {
        pending[f.name] = {
          semantic_type: f.semantic_type === "unknown" ? "dimension" : f.semantic_type,
          meaning: f.meaning || "",
          ignored: f.ignored,
        };
      }
    }
    setDecisions(pending);
  }

  const pendingNames = useMemo(
    () => (dictionary ? dictionary.fields.filter((f) => !!decisions[f.name]).map((f) => f.name) : []),
    [dictionary, decisions],
  );

  const generate = async () => {
    setLocalError(null);
    setGenerating(true);
    try {
      const r = await api.generateProfile(sessionId);
      initFromDictionary(r.dictionary);
    } catch (e) {
      setLocalError(e instanceof ApiError ? e.message : "语义分析失败。");
    } finally {
      setGenerating(false);
    }
  };

  const submitConfirm = async () => {
    setLocalError(null);
    setSubmitting(true);
    try {
      const r = await api.confirmFields(
        sessionId,
        Object.entries(decisions).map(([name, d]) => ({
          name,
          semantic_type: d.ignored ? undefined : d.semantic_type,
          meaning: d.ignored ? undefined : d.meaning || "用户确认",
          ignored: d.ignored,
        })),
      );
      setDictionary(r.dictionary);
      setDecisions({});
      await onAdvance();
      onJump(2);
    } catch (e) {
      setLocalError(e instanceof ApiError ? e.message : "字段确认失败。");
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return <CenterSpinner />;
  }

  if (!dictionary) {
    return (
      <Card>
        <SectionTitle
          title="语义分析（Semantic Profile）"
          desc="AI 会结合字段名、样例值与统计特征识别字段含义；无法确认的字段需要你逐项确认，确认结果将写入数据字典。"
        />
        {localError && <LocalError message={localError} onRetry={generate} />}
        <Button loading={generating} onClick={generate}>
          开始语义分析
        </Button>
      </Card>
    );
  }

  const allConfirmed = dictionary.complete;

  return (
    <div className="space-y-4">
      <Card>
        <SectionTitle
          title="数据字典与语义确认"
          desc={`共 ${dictionary.fields.length} 个字段。绿色徽标=AI 高置信识别；黄色/红色=需要你确认后才会进入后续分析。`}
        />
        {localError && <LocalError message={localError} />}
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-zinc-200 text-left text-xs text-zinc-400">
                <th className="py-2 pr-3 font-medium">字段</th>
                <th className="py-2 pr-3 font-medium">物理类型</th>
                <th className="py-2 pr-3 font-medium">语义 / 含义</th>
                <th className="py-2 pr-3 font-medium">样例</th>
                <th className="py-2 pr-3 font-medium">置信度</th>
                {!allConfirmed && <th className="py-2 pr-3 font-medium">你的确认</th>}
              </tr>
            </thead>
            <tbody>
              {dictionary.fields.map((f) => (
                <FieldRow
                  key={f.name}
                  field={f}
                  pending={decisions[f.name]}
                  allConfirmed={allConfirmed}
                  onChange={(d) => setDecisions((s) => ({ ...s, [f.name]: d }))}
                />
              ))}
            </tbody>
          </table>
        </div>

        {dictionary.relations.length > 0 && (
          <div className="mt-4 rounded-lg bg-zinc-50 p-3 text-xs text-zinc-500">
            <p className="mb-1 font-medium text-zinc-600">自动识别的字段关系</p>
            <ul className="list-inside list-disc space-y-0.5">
              {dictionary.relations.map((r, i) => (
                <li key={i}>{r.note}</li>
              ))}
            </ul>
          </div>
        )}

        <div className="mt-5 flex items-center gap-3">
          {!allConfirmed ? (
            <>
              <Button
                loading={submitting}
                disabled={pendingNames.length === 0 || pendingNames.some(
                  (n) => !decisions[n].ignored && !decisions[n].meaning.trim(),
                )}
                onClick={submitConfirm}
              >
                提交确认{pendingNames.length > 0 ? `（${pendingNames.length} 项）` : ""}
              </Button>
              <span className="text-xs text-zinc-400">
                所有待确认字段处理完后才能进入下一步（可选择「忽略字段」）
              </span>
            </>
          ) : (
            !readOnly && (
              <Button onClick={() => onJump(2)}>
                下一步：数据质量检测 →
              </Button>
            )
          )}
        </div>
      </Card>
    </div>
  );
}

function FieldRow({
  field: f,
  pending,
  allConfirmed,
  onChange,
}: {
  field: FieldProfile;
  pending?: { semantic_type: SemanticType; meaning: string; ignored: boolean };
  allConfirmed: boolean;
  onChange: (d: { semantic_type: SemanticType; meaning: string; ignored: boolean }) => void;
}) {
  const tone = CONF_TONE[f.confidence];
  const examples = f.examples.slice(0, 3).map((v) => (v === null ? "∅" : String(v))).join("，");
  return (
    <tr className="border-b border-zinc-100 align-top">
      <td className="py-3 pr-3 font-medium text-zinc-800">{f.name}</td>
      <td className="py-3 pr-3 text-xs text-zinc-500">{f.physical_type}</td>
      <td className="py-3 pr-3">
        <Badge tone={f.semantic_type === "unknown" ? "amber" : "blue"}>
          {SEMANTIC_LABELS[f.semantic_type]}
        </Badge>
        <p className="mt-1 text-xs text-zinc-600">{f.meaning || "—"}{f.unit ? `（单位：${f.unit}）` : ""}</p>
        {!allConfirmed && f.candidates.length > 0 && !pending?.ignored && (
          <p className="mt-1 text-xs text-zinc-400">候选：{f.candidates.join(" / ")}</p>
        )}
      </td>
      <td className="max-w-[180px] truncate py-3 pr-3 text-xs text-zinc-500" title={examples}>
        {examples || "—"}
      </td>
      <td className="py-3 pr-3">
        <Badge tone={tone}>
          {f.confidence === "high" ? "高" : f.confidence === "medium" ? "待确认" : "低置信"}
        </Badge>
        {f.confirmed_by_user && <span className="ml-1 text-xs text-emerald-600">已确认</span>}
      </td>
      {!allConfirmed && (
        <td className="min-w-[260px] py-3 pr-3">
          {pending ? (
            <div className="space-y-2">
              {f.candidates.length > 0 && !pending.ignored && (
                <div className="flex flex-wrap gap-1">
                  {f.candidates.map((c) => (
                    <button
                      key={c}
                      type="button"
                      onClick={() => onChange({ ...pending, meaning: c })}
                      className={`rounded-full border px-2 py-0.5 text-xs ${
                        pending.meaning === c
                          ? "border-accent bg-accent-soft text-accent"
                          : "border-zinc-200 text-zinc-500 hover:bg-zinc-50"
                      }`}
                    >
                      {c}
                    </button>
                  ))}
                </div>
              )}
              {!pending.ignored && (
                <div className="flex gap-2">
                  <select
                    value={pending.semantic_type}
                    onChange={(e) =>
                      onChange({ ...pending, semantic_type: e.target.value as SemanticType })
                    }
                    className="rounded-md border border-zinc-300 px-2 py-1 text-xs"
                  >
                    {SEMANTIC_OPTIONS.map((s) => (
                      <option key={s} value={s}>
                        {SEMANTIC_LABELS[s]}
                      </option>
                    ))}
                  </select>
                  <input
                    value={pending.meaning}
                    onChange={(e) => onChange({ ...pending, meaning: e.target.value })}
                    placeholder="确认字段业务含义"
                    className="min-w-0 flex-1 rounded-md border border-zinc-300 px-2 py-1 text-xs"
                  />
                </div>
              )}
              <label className="flex items-center gap-1 text-xs text-zinc-500">
                <input
                  type="checkbox"
                  checked={pending.ignored}
                  onChange={(e) => onChange({ ...pending, ignored: e.target.checked })}
                />
                忽略该字段（不参与分析）
              </label>
            </div>
          ) : (
            <span className="text-xs text-emerald-600">✓ 已自动识别</span>
          )}
        </td>
      )}
    </tr>
  );
}

function LocalError({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="mb-4">
      <ErrorBanner message={message} onRetry={onRetry} />
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
