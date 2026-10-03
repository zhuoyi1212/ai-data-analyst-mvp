"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { Button, ErrorBanner, Reveal, Spinner } from "@/components/ui";

const MAX_MB = 20;
const ACCEPTED = ".csv,.xlsx,.xls";

const SAMPLE_HINTS: Record<string, string> = {
  sales_orders: "销售订单 · 3004 行 · 混合日期 / 缺失 / 离群 / 重复",
  operations_daily: "运营日报 · 174 天 · 哨兵值 / 缺口 / 峰值",
  marketing_campaigns: "营销投放 · 2164 行 · 货币文本 / 枚举大小写",
  inventory_movements: "库存流水 · 2163 行 · 负库存 / 缺失 / 重复",
  customer_tickets: "客服工单 · 2600 行 · 时长离群 / 多渠道",
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
    <div className="min-h-screen bg-white">
      {/* ------------------------------------------------ 顶部导航 */}
      <header className="glass-bar fixed inset-x-0 top-0 z-40">
        <div className="mx-auto flex h-12 max-w-6xl items-center justify-between px-6">
          <span className="text-sm font-semibold tracking-tight text-ink">
            AI Data Analyst
          </span>
          <nav className="flex items-center gap-6 text-xs text-muted">
            <a href="#features" className="transition-colors hover:text-ink">
              设计原则
            </a>
            <a href="#samples" className="transition-colors hover:text-ink">
              示例数据集
            </a>
          </nav>
        </div>
      </header>

      <main>
        {/* ------------------------------------------------ Hero */}
        <section className="relative overflow-hidden px-6 pb-24 pt-36 sm:pt-44">
          {/* 微妙的环境光晕 */}
          <div
            aria-hidden
            className="pointer-events-none absolute inset-x-0 top-[-20%] h-[680px]"
            style={{
              background:
                "radial-gradient(60% 60% at 50% 30%, rgba(0,113,227,0.10) 0%, rgba(124,77,255,0.05) 40%, rgba(255,255,255,0) 72%)",
            }}
          />
          <div className="relative mx-auto max-w-4xl text-center">
            <p
              className="animate-fade-up text-sm font-medium text-apple"
              style={{ animationDelay: "0ms" }}
            >
              AI Data Analyst
            </p>
            <h1
              className="animate-fade-up mt-5 text-5xl font-semibold leading-[1.07] tracking-[-0.03em] text-ink sm:text-6xl lg:text-7xl"
              style={{ animationDelay: "90ms" }}
            >
              让每个人，
              <br className="hidden sm:block" />
              都能做专业的数据分析。
            </h1>
            <p
              className="animate-fade-up mx-auto mt-7 max-w-2xl text-lg leading-relaxed text-muted"
              style={{ animationDelay: "180ms" }}
            >
              无需 SQL，也无需 Python。上传一份二维表格，AI
              会自动理解数据、多维扫描、对关键信号逐层深挖，
              产出交互式 Dashboard 与深度分析报告。
              所有数值由确定性计算引擎产出，每条结论都可追溯到数据与公式。
            </p>
            <div
              className="animate-fade-up mt-10 flex flex-wrap items-center justify-center gap-x-8 gap-y-4"
              style={{ animationDelay: "280ms" }}
            >
              <Button size="lg" onClick={() => inputRef.current?.click()}>
                上传一份数据
              </Button>
              <a href="#samples" className="link-apple text-lg">
                查看示例数据集 <span aria-hidden>›</span>
              </a>
            </div>
          </div>
        </section>

        {/* ------------------------------------------------ 上传区 */}
        <section id="upload" className="bg-canvas px-6 py-24">
          <div className="mx-auto max-w-3xl">
            <Reveal>
              <div className="text-center">
                <h2 className="text-4xl font-semibold tracking-[-0.02em] text-ink sm:text-5xl">
                  拖入，即开始。
                </h2>
                <p className="mx-auto mt-4 max-w-xl text-base leading-relaxed text-muted">
                  文件仅在本地解析，不写入任何外部服务。支持 CSV 与
                  Excel，单个文件不超过 {MAX_MB}MB。
                </p>
              </div>
            </Reveal>

            <Reveal delay={120}>
              <div className="mt-12">
                {error && (
                  <div className="mb-5">
                    <ErrorBanner
                      message={error}
                      onRetry={() => setError(null)}
                    />
                  </div>
                )}
                <div
                  role="button"
                  tabIndex={0}
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
                  onClick={() => inputRef.current?.click()}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      inputRef.current?.click();
                    }
                  }}
                  className={`group cursor-pointer rounded-[2rem] border-2 border-dashed bg-white px-10 py-16 text-center transition-all duration-300 ease-apple-out
                    ${dragging
                      ? "scale-[1.01] border-apple bg-apple-tint shadow-lift"
                      : "border-line hover:border-faint hover:shadow-card"
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
                    <div className="flex flex-col items-center gap-4 text-muted">
                      <Spinner className="h-7 w-7 text-apple" />
                      <p className="text-sm">正在解析文件…</p>
                    </div>
                  ) : (
                    <div className="flex flex-col items-center">
                      <span
                        className={`flex h-16 w-16 items-center justify-center rounded-2xl bg-apple-soft text-apple transition-transform duration-300 ease-apple-out
                          ${dragging ? "scale-110" : "group-hover:-translate-y-0.5"}`}
                      >
                        <svg
                          className="h-7 w-7"
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.8"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        >
                          <path d="M12 16V4m0 0L7 9m5-5l5 5" />
                          <path d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2" />
                        </svg>
                      </span>
                      <p className="mt-6 text-base font-medium text-ink">
                        {dragging ? "松开，立即上传" : "拖拽文件到此处"}
                      </p>
                      <p className="mt-1 text-sm text-muted">
                        或点击选择文件
                      </p>
                      <p className="mt-5 text-xs text-faint">
                        .csv / .xlsx / .xls · 不超过 20 万行 × 50 列
                      </p>
                    </div>
                  )}
                </div>
              </div>
            </Reveal>
          </div>
        </section>

        {/* ------------------------------------------------ 设计原则 */}
        <section id="features" className="px-6 py-24">
          <div className="mx-auto max-w-6xl">
            <Reveal>
              <h2 className="text-center text-4xl font-semibold tracking-[-0.02em] text-ink sm:text-5xl">
                AI 负责理解。
                <br />
                数字，交给确定性。
              </h2>
            </Reveal>

            <div className="mt-16 grid gap-6 md:grid-cols-3">
              {FEATURES.map((f, i) => (
                <Reveal key={f.title} delay={i * 110}>
                  <div className="h-full rounded-3xl bg-canvas p-8">
                    <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-white text-apple shadow-card">
                      {f.icon}
                    </span>
                    <h3 className="mt-6 text-lg font-semibold tracking-tight text-ink">
                      {f.title}
                    </h3>
                    <p className="mt-2 text-sm leading-relaxed text-muted">
                      {f.desc}
                    </p>
                  </div>
                </Reveal>
              ))}
            </div>
          </div>
        </section>

        {/* ------------------------------------------------ 示例数据集 */}
        <section id="samples" className="bg-canvas px-6 py-24">
          <div className="mx-auto max-w-6xl">
            <Reveal>
              <div className="text-center">
                <h2 className="text-4xl font-semibold tracking-[-0.02em] text-ink sm:text-5xl">
                  没有数据？
                  <br />
                  从一个真实场景开始。
                </h2>
                <p className="mx-auto mt-4 max-w-xl text-base leading-relaxed text-muted">
                  每个示例集都预埋了常见的数据质量问题，
                  可完整体验 AI 自动体检与分析的完整流程。
                </p>
              </div>
            </Reveal>

            <div className="mt-14 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
              {samples.length === 0 && (
                <div className="flex items-center gap-3 text-sm text-muted sm:col-span-2 lg:col-span-3">
                  <Spinner className="h-4 w-4 text-apple" />
                  正在加载示例列表…
                </div>
              )}
              {samples.map((name, i) => {
                const stem = name.replace(/\.(csv|xlsx|xls)$/, "");
                const busy = loading === name;
                return (
                  <Reveal key={name} delay={i * 80}>
                    <button
                      type="button"
                      disabled={loading !== null}
                      onClick={() => void loadSample(name)}
                      className="group flex h-full w-full flex-col rounded-3xl bg-white p-7 text-left shadow-card transition-all duration-300 ease-apple-out hover:-translate-y-1 hover:shadow-lift disabled:pointer-events-none disabled:opacity-50"
                    >
                      <span className="text-base font-semibold tracking-tight text-ink">
                        {stem}
                      </span>
                      <span className="mt-2 flex-1 text-sm leading-relaxed text-muted">
                        {SAMPLE_HINTS[stem] ?? name}
                      </span>
                      <span className="mt-6 inline-flex items-center gap-1 text-sm font-medium text-apple">
                        {busy ? (
                          <>
                            <Spinner className="h-4 w-4" /> 正在载入…
                          </>
                        ) : (
                          <>
                            载入此数据集
                            <span className="transition-transform duration-300 group-hover:translate-x-0.5">
                              →
                            </span>
                          </>
                        )}
                      </span>
                    </button>
                  </Reveal>
                );
              })}
            </div>
          </div>
        </section>
      </main>

      {/* ------------------------------------------------ 页脚 */}
      <footer className="px-6 py-14">
        <div className="mx-auto flex max-w-6xl flex-col items-center gap-3 text-center">
          <p className="text-xs text-faint">
            问题 → 分析 → 图表 → 洞察 → 证据
          </p>
          <p className="max-w-lg text-xs leading-relaxed text-faint">
            LLM 只负责理解与表达；数值计算与校验全部由确定性引擎完成。
          </p>
        </div>
      </footer>
    </div>
  );
}

// ---------------------------------------------------------------- 特性数据

const iconProps = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.7,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  className: "h-6 w-6",
};

const FEATURES = [
  {
    title: "确定性计算引擎",
    desc: "全部数值由 pandas 在固定算子集内计算产出。同一个问题、同一份数据，无论运行多少次，结果完全一致。",
    icon: (
      <svg {...iconProps}>
        <path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6l7-3z" />
        <path d="M9.5 12l1.8 1.8 3.4-3.6" />
      </svg>
    ),
  },
  {
    title: "每个数字都可追溯",
    desc: "每一步的口径、公式、参与行数与中间结果全部留痕。洞察中的数字可以逐级回算到原始数据。",
    icon: (
      <svg {...iconProps}>
        <circle cx="11" cy="11" r="6.5" />
        <path d="M16 16l4 4M11 8.5V11l2 1.5" />
      </svg>
    ),
  },
  {
    title: "AI 守在边界内",
    desc: "LLM 不产数字、不写公式、不生成代码；它只理解你的意图、组织表达。无法支持的分析，会明确告诉你。",
    icon: (
      <svg {...iconProps}>
        <path d="M12 3l1.9 3.9 4.3.6-3.1 3 .8 4.3L12 12.7 8.1 14.8l.8-4.3-3.1-3 4.3-.6L12 3z" />
      </svg>
    ),
  },
];
