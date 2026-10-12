"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import type { SessionSummary } from "@/lib/types";
import { Button, ErrorBanner, Spinner } from "@/components/ui";

const MAX_MB = 20;
const ACCEPTED = ".csv,.xlsx,.xls";

const STATUS: Record<string, { label: string; dot: string; text: string }> = {
  completed: {
    label: "已生成仪表板",
    dot: "bg-success-dot",
    text: "text-success",
  },
  running: { label: "分析中", dot: "bg-apple", text: "text-apple" },
  waiting_input: {
    label: "待补充信息",
    dot: "bg-warning-dot",
    text: "text-warning",
  },
  failed: { label: "分析失败", dot: "bg-danger-dot", text: "text-danger" },
  upload: { label: "待分析", dot: "bg-faint", text: "text-faint" },
};

function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const diff = Date.now() - d.getTime();
  const min = Math.floor(diff / 60000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h} 小时前`;
  const days = Math.floor(h / 24);
  if (days < 7) return `${days} 天前`;
  return d.toLocaleDateString();
}

export default function ProjectsPage() {
  const router = useRouter();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const load = useCallback(() => {
    api
      .listSessions()
      .then((r) => setSessions(r.sessions))
      .catch((e: ApiError) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const openSession = useCallback(
    (id: string, status: string) => {
      router.push(
        status === "completed"
          ? `/sessions/${id}/workspace`
          : `/sessions/${id}/analyzing`,
      );
    },
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
      try {
        const { meta } = await api.upload(file);
        router.push(`/sessions/${meta.session_id}/analyzing`);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "上传失败，请重试。");
      }
    },
    [router],
  );

  const handleDelete = useCallback(async (id: string) => {
    setError(null);
    setDeleting(true);
    try {
      await api.deleteSession(id);
      setSessions((prev) => prev.filter((s) => s.session_id !== id));
      setConfirmDelete(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "删除失败，请重试。");
    } finally {
      setDeleting(false);
    }
  }, []);

  return (
    <div className="min-h-screen bg-white">
      {/* ------------------------------------------------ 顶部导航 */}
      <header className="glass-bar sticky top-0 z-40">
        <div className="mx-auto flex h-12 max-w-3xl items-center justify-between px-6">
          <Link
            href="/"
            className="text-sm font-semibold tracking-tight text-ink"
          >
            AI Data Analyst
          </Link>
          <Link
            href="/"
            className="text-xs font-medium text-muted transition-colors hover:text-ink"
          >
            返回首页
          </Link>
        </div>
      </header>

      {/* ------------------------------------------------ 项目列表 */}
      <main className="mx-auto max-w-3xl px-6 pb-24 pt-10">
        <div className="flex items-end justify-between gap-4">
          <h1 className="text-3xl font-bold tracking-[-0.02em] text-ink">
            项目
          </h1>
          <Button
            variant="secondary"
            onClick={() => inputRef.current?.click()}
          >
            ＋ 新建项目
          </Button>
        </div>
        <p className="mt-1 text-sm text-muted">共 {sessions.length} 个项目</p>

        {error && (
          <div className="mt-6">
            <ErrorBanner message={error} onRetry={() => setError(null)} />
          </div>
        )}

        <div className="mt-8">
          {loading ? (
            <div className="flex justify-center py-20">
              <Spinner className="h-6 w-6 text-apple" />
            </div>
          ) : sessions.length === 0 ? (
            <p className="py-20 text-center text-sm text-muted">
              还没有项目，上传一份数据开始。
            </p>
          ) : (
            <div className="space-y-3">
              {sessions.map((s) => {
                const st = STATUS[s.status] ?? STATUS.upload;
                const isConfirm = confirmDelete === s.session_id;
                return (
                  <div
                    key={s.session_id}
                    className="flex items-center justify-between gap-4 rounded-2xl border border-hairline bg-white px-5 py-4 transition-all duration-200 hover:-translate-y-0.5 hover:shadow-card"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2.5">
                        <span
                          className={`inline-block h-2 w-2 shrink-0 rounded-full ${st.dot}`}
                        />
                        <span className="truncate text-[15px] font-semibold tracking-[-0.01em] text-ink">
                          {s.filename}
                        </span>
                        <span
                          className={`shrink-0 text-xs font-medium ${st.text}`}
                        >
                          {st.label}
                        </span>
                      </div>
                      <p className="mt-1.5 truncate text-xs text-faint">
                        {s.shape.rows.toLocaleString()} 行 × {s.shape.cols} 列
                        {" · "}
                        {fmtTime(s.created_at)}
                      </p>
                    </div>
                    <div className="flex shrink-0 items-center gap-1.5">
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => openSession(s.session_id, s.status)}
                      >
                        打开
                      </Button>
                      {isConfirm ? (
                        <>
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={deleting}
                            onClick={() => setConfirmDelete(null)}
                          >
                            取消
                          </Button>
                          <Button
                            size="sm"
                            variant="danger"
                            loading={deleting}
                            onClick={() => void handleDelete(s.session_id)}
                          >
                            确认删除
                          </Button>
                        </>
                      ) : (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => setConfirmDelete(s.session_id)}
                        >
                          删除
                        </Button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

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
    </div>
  );
}
