"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { Button, ErrorBanner, Spinner } from "@/components/ui";

const MAX_MB = 20;
const ACCEPTED = ".csv,.xlsx,.xls";

const SAMPLE_HINTS: Record<string, string> = {
  sales_orders: "销售订单 · 3004 行 · 混合日期/缺失/离群/重复",
  operations_daily: "运营日报 · 174 天 · 哨兵值/缺口/峰值",
  marketing_campaigns: "营销投放 · 2164 行 · 货币文本/枚举大小写",
  inventory_movements: "库存流水 · 2163 行 · 负库存/缺失/重复",
  customer_tickets: "客服工单 · 2600 行 · 时长离群/多渠道",
};

export default function Home() {
  const router = useRouter();
  const [samples, setSamples] = useState<string[]>([]);
  const [loading, setLoading] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api
      .listSamples()
      .then((r) => setSamples(r.samples))
      .catch((e: ApiError) => setError(e.message));
  }, []);

  const go = useCallback(
    (sessionId: string) => router.push(`/sessions/${sessionId}`),
    [router],
  );

  const handleFile = useCallback(
    async (file: File) => {
      setError(null);
      const lower = file.name.toLowerCase();
      if (!lower.endsWith(".csv") && !lower.endsWith(".xlsx") && !lower.endsWith(".xls")) {
        setError("仅支持标准二维 CSV / Excel 文件（.csv / .xlsx / .xls）。");
        return;
      }
      if (file.size > MAX_MB * 1024 * 1024) {
        setError(`文件大小 ${(file.size / 1024 / 1024).toFixed(1)}MB，超过 ${MAX_MB}MB 上限。`);
        return;
      }
      setLoading("upload");
      try {
        const { meta } = await api.upload(file);
        go(meta.session_id);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "上传失败，请重试。");
        setLoading(null);
      }
    },
    [go],
  );

  const loadSample = useCallback(
    async (filename: string) => {
      setError(null);
      setLoading(filename);
      try {
        const { meta } = await api.fromSample(filename);
        go(meta.session_id);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "示例载入失败，请重试。");
        setLoading(null);
      }
    },
    [go],
  );

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col justify-center gap-8 px-6 py-16">
      <header className="space-y-3">
        <h1 className="text-3xl font-semibold tracking-tight text-zinc-900">
          AI Data Analyst
        </h1>
        <p className="max-w-xl text-zinc-500">
          不会 SQL / Python 也可以完成专业的数据分析。上传一份二维表格，AI
          会与你逐步确认字段语义、处理数据质量、规划分析方案；所有数值都由确定性计算引擎产出，
          每条洞察都可追溯到数据与公式。
        </p>
      </header>

      {error && <ErrorBanner message={error} onRetry={() => setError(null)} />}

      <section
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          const f = e.dataTransfer.files?.[0];
          if (f) void handleFile(f);
        }}
        className={`rounded-2xl border-2 border-dashed bg-white p-10 text-center transition-colors ${
          dragging ? "border-accent bg-accent-soft" : "border-zinc-300"
        }`}
      >
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPTED}
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void handleFile(f);
            e.target.value = "";
          }}
        />
        {loading === "upload" ? (
          <div className="flex items-center justify-center gap-3 text-zinc-500">
            <Spinner /> 正在解析文件…
          </div>
        ) : (
          <div className="space-y-4">
            <p className="text-sm font-medium text-zinc-800">拖拽 CSV / Excel 到此处，或</p>
            <Button onClick={() => inputRef.current?.click()}>选择文件上传</Button>
            <p className="text-xs text-zinc-400">
              支持 .csv / .xlsx / .xls，单个文件 ≤ {MAX_MB}MB，不超过 20 万行 × 50 列
            </p>
          </div>
        )}
      </section>

      <section className="space-y-3">
        <div>
          <h2 className="text-sm font-semibold text-zinc-800">或从示例数据集开始</h2>
          <p className="mt-0.5 text-xs text-zinc-400">
            示例数据预埋了常见质量问题，可完整体验「检测 → 确认 → 处理」流程
          </p>
        </div>
        <div className="grid gap-2 sm:grid-cols-2">
          {samples.length === 0 && (
            <div className="flex items-center gap-2 text-sm text-zinc-400 sm:col-span-2">
              <Spinner className="h-4 w-4" /> 正在加载示例列表…
            </div>
          )}
          {samples.map((name) => {
            const stem = name.replace(/\.(csv|xlsx|xls)$/, "");
            return (
              <button
                key={name}
                type="button"
                disabled={loading !== null}
                onClick={() => void loadSample(name)}
                className="flex items-center justify-between gap-3 rounded-lg border border-zinc-200 bg-white px-4 py-3 text-left transition-colors hover:border-accent hover:bg-accent-soft disabled:opacity-50"
              >
                <span>
                  <span className="block text-sm font-medium text-zinc-800">{stem}</span>
                  <span className="mt-0.5 block text-xs text-zinc-400">
                    {SAMPLE_HINTS[stem] ?? name}
                  </span>
                </span>
                {loading === name ? <Spinner className="h-4 w-4 text-accent" /> : (
                  <span className="text-xs text-accent">载入 →</span>
                )}
              </button>
            );
          })}
        </div>
      </section>

      <footer className="text-center text-xs text-zinc-400">
        问题 → 分析 → 图表 → 洞察 → 证据 · LLM 只负责理解与表达，数值计算与校验全部由确定性引擎完成
      </footer>
    </main>
  );
}
