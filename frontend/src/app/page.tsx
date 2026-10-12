"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { Button, ErrorBanner } from "@/components/ui";

const MAX_MB = 20;
const ACCEPTED = ".csv,.xlsx,.xls";

const SAMPLE_SHORT: Record<string, string> = {
  sales_orders: "销售订单",
  operations_daily: "运营日报",
  marketing_campaigns: "营销投放",
  inventory_movements: "库存流水",
  customer_tickets: "客服工单",
};

export default function Home() {
  const router = useRouter();
  const [samples, setSamples] = useState<string[]>([]);
  const [projectCount, setProjectCount] = useState(0);
  const [loading, setLoading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api
      .listSamples()
      .then((r) => setSamples(r.samples))
      .catch(() => {});
    api
      .listSessions()
      .then((r) => setProjectCount(r.sessions.length))
      .catch(() => {});
  }, []);

  const go = useCallback(
    (sessionId: string) => router.push(`/sessions/${sessionId}/analyzing`),
    [router],
  );

  const handleFile = useCallback(
    async (file: File) => {
      setError(null);
      const lower = file.name.toLowerCase();
      if (
        !lower.endsWith(".csv") &&
        !lower.endsWith(".xlsx") &&
        !lower.endsWith(".xls")
      ) {
        setError("仅支持标准二维 CSV / Excel 文件（.csv / .xlsx / .xls）。");
        return;
      }
      if (file.size > MAX_MB * 1024 * 1024) {
        setError(
          `文件大小 ${(file.size / 1024 / 1024).toFixed(1)}MB，超过 ${MAX_MB}MB 上限。`,
        );
        return;
      }
      setLoading(true);
      try {
        const { meta } = await api.upload(file);
        go(meta.session_id);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "上传失败，请重试。");
        setLoading(false);
      }
    },
    [go],
  );

  const loadSample = useCallback(
    async (filename: string) => {
      setError(null);
      setLoading(true);
      try {
        const { meta } = await api.fromSample(filename);
        go(meta.session_id);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "示例载入失败，请重试。");
        setLoading(false);
      }
    },
    [go],
  );

  return (
    <div className="flex min-h-screen flex-col bg-white">
      {/* ------------------------------------------------ 顶部导航 */}
      <header className="glass-bar sticky top-0 z-40">
        <div className="mx-auto flex h-12 max-w-6xl items-center px-6">
          <Link
            href="/"
            className="text-sm font-semibold tracking-tight text-ink"
          >
            AI Data Analyst
          </Link>
        </div>
      </header>

      {/* ------------------------------------------------ 唯一主行动 */}
      <main
        className={`flex flex-1 flex-col items-center justify-center px-6 py-16 text-center transition-colors duration-200 ${
          dragging ? "bg-canvas" : ""
        }`}
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
      >
        {error && (
          <div className="mb-6 w-full max-w-md">
            <ErrorBanner message={error} onRetry={() => setError(null)} />
          </div>
        )}

        <h1 className="text-[clamp(3rem,7vw,4.75rem)] font-bold leading-[1.04] tracking-[-0.03em] text-ink">
          从表格，到答案。
        </h1>

        <p className="mx-auto mt-6 max-w-md text-lg leading-relaxed text-muted">
          无需 SQL，也无需 Python。上传一份表格，AI
          自动理解数据、发现信号、生成仪表板。
        </p>

        <div className="mt-10 flex flex-wrap items-center justify-center gap-3">
          <Button
            size="lg"
            loading={loading}
            onClick={() => inputRef.current?.click()}
          >
            上传数据
          </Button>
          <Button
            size="lg"
            variant="secondary"
            onClick={() => router.push("/projects")}
          >
            历史项目{projectCount > 0 ? ` (${projectCount})` : ""}
          </Button>
        </div>

        <p className="mt-5 text-xs text-faint">
          拖入文件到此处，或点击上传 · .csv .xlsx .xls · 不超过 {MAX_MB}MB
        </p>

        {samples.length > 0 && (
          <p className="mt-10 text-sm text-muted">
            没有数据？试一个示例：
            {samples.map((name, i) => {
              const stem = name.replace(/\.(csv|xlsx|xls)$/, "");
              const label = SAMPLE_SHORT[stem] ?? stem;
              return (
                <span key={name}>
                  {i > 0 && <span className="text-faint"> · </span>}
                  <button
                    type="button"
                    disabled={loading}
                    onClick={() => void loadSample(name)}
                    className="text-apple transition-colors hover:underline disabled:opacity-40"
                  >
                    {label}
                  </button>
                </span>
              );
            })}
          </p>
        )}

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
      </main>

      {/* ------------------------------------------------ 页脚 */}
      <footer className="px-6 py-6 text-center text-xs text-faint">
        数值由确定性引擎计算 · 每个结论都可回算到原始数据
      </footer>
    </div>
  );
}
