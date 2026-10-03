"use client";

import {
  ButtonHTMLAttributes,
  ReactNode,
  useEffect,
  useRef,
  useState,
} from "react";

// ================================================================ Button

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md" | "lg";

const VARIANT_CLASSES: Record<Variant, string> = {
  primary:
    "bg-apple text-white shadow-sm hover:bg-apple-hover active:bg-apple-active",
  secondary:
    "bg-canvas text-ink hover:bg-line/70 active:bg-line",
  ghost: "text-muted hover:bg-canvas hover:text-ink active:bg-line/60",
  danger:
    "border border-danger/25 bg-white text-danger hover:bg-danger-soft active:bg-danger/10",
};

const SIZE_CLASSES: Record<Size, string> = {
  sm: "px-3.5 py-1.5 text-xs gap-1.5",
  md: "px-5 py-2.5 text-sm gap-2",
  lg: "px-8 py-3.5 text-base gap-2",
};

export function Button({
  variant = "primary",
  size = "md",
  loading = false,
  children,
  className = "",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
}) {
  return (
    <button
      {...rest}
      disabled={rest.disabled || loading}
      className={`inline-flex select-none items-center justify-center rounded-full font-medium
        transition-all duration-200 ease-apple active:scale-[0.97]
        disabled:pointer-events-none disabled:opacity-40
        ${VARIANT_CLASSES[variant]} ${SIZE_CLASSES[size]} ${className}`}
    >
      {loading && <Spinner className="h-4 w-4" />}
      {children}
    </button>
  );
}

// ================================================================ Card

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`rounded-3xl border border-hairline bg-white p-6 shadow-card sm:p-8 ${className}`}
    >
      {children}
    </div>
  );
}

export function SectionTitle({
  title,
  desc,
}: {
  title: string;
  desc?: string;
}) {
  return (
    <div className="mb-5">
      <h2 className="text-xl font-semibold tracking-tight text-ink">
        {title}
      </h2>
      {desc && (
        <p className="mt-1.5 text-sm leading-relaxed text-muted">{desc}</p>
      )}
    </div>
  );
}

// ================================================================ Badge

type BadgeTone =
  | "neutral"
  | "blue"
  | "amber"
  | "green"
  | "red"
  | "violet";

const TONE_CLASSES: Record<BadgeTone, string> = {
  neutral: "bg-canvas text-muted",
  blue: "bg-apple-soft text-apple",
  amber: "bg-warning-soft text-warning",
  green: "bg-success-soft text-success",
  red: "bg-danger-soft text-danger",
  violet: "bg-violet-soft text-violet",
};

export function Badge({
  children,
  tone = "neutral",
  className = "",
}: {
  children: ReactNode;
  tone?: BadgeTone;
  className?: string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium ${TONE_CLASSES[tone]} ${className}`}
    >
      {children}
    </span>
  );
}

// ================================================================ Spinner

export function Spinner({ className = "h-5 w-5" }: { className?: string }) {
  return (
    <svg
      className={`animate-spin text-current ${className}`}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden
    >
      <circle
        className="opacity-20"
        cx="12"
        cy="12"
        r="10"
        stroke="currentColor"
        strokeWidth="3.5"
      />
      <path
        className="opacity-90"
        fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.4 0 0 5.4 0 12h4z"
      />
    </svg>
  );
}

// ================================================================ Banner

export function ErrorBanner({
  message,
  onRetry,
  title = "操作未完成",
}: {
  message: string;
  onRetry?: () => void;
  title?: string;
}) {
  return (
    <div className="flex items-start justify-between gap-4 rounded-2xl border border-danger/15 bg-danger-soft px-5 py-4">
      <div className="flex items-start gap-3">
        <svg
          className="mt-0.5 h-5 w-5 shrink-0 text-danger"
          viewBox="0 0 20 20"
          fill="currentColor"
        >
          <path
            fillRule="evenodd"
            d="M10 18a8 8 0 100-16 8 8 0 000 16zm.75-11.5a.75.75 0 00-1.5 0v4a.75.75 0 001.5 0v-4zM10 14a1 1 0 100 2 1 1 0 000-2z"
            clipRule="evenodd"
          />
        </svg>
        <div>
          <p className="text-sm font-semibold text-danger">{title}</p>
          <p className="mt-0.5 whitespace-pre-wrap text-sm leading-relaxed text-danger/85">
            {message}
          </p>
        </div>
      </div>
      {onRetry && (
        <Button
          variant="danger"
          size="sm"
          onClick={onRetry}
          className="shrink-0"
        >
          重试
        </Button>
      )}
    </div>
  );
}

// ================================================================ Empty

export function EmptyState({
  title,
  desc,
}: {
  title: string;
  desc?: string;
}) {
  return (
    <div className="rounded-3xl bg-canvas px-8 py-14 text-center">
      <p className="text-base font-medium text-ink">{title}</p>
      {desc && (
        <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-muted">
          {desc}
        </p>
      )}
    </div>
  );
}

// ================================================================ KV

export function KeyValue({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="flex gap-3 text-sm">
      <span className="shrink-0 text-faint">{k}</span>
      <span className="text-ink">{v}</span>
    </div>
  );
}

// ================================================================ Reveal

/** 进入视口时淡入上浮；尊重 reduced-motion（由 CSS 兜底）。 */
export function Reveal({
  children,
  delay = 0,
  className = "",
}: {
  children: ReactNode;
  delay?: number;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [shown, setShown] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setShown(true);
          io.disconnect();
        }
      },
      { threshold: 0.12, rootMargin: "0px 0px -8% 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  return (
    <div
      ref={ref}
      className={className}
      style={{
        opacity: shown ? undefined : 0,
        animation: shown
          ? `fade-up 0.8s cubic-bezier(0.16,1,0.3,1) ${delay}ms both`
          : undefined,
      }}
    >
      {children}
    </div>
  );
}
