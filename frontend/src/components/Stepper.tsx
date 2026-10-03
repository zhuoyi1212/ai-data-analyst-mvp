"use client";

import { WORKFLOW_STEPS } from "@/lib/workflow";

export function Stepper({
  current,
  reached,
  onJump,
}: {
  current: number;
  reached: number;
  onJump?: (index: number) => void;
}) {
  return (
    <nav className="rounded-3xl bg-canvas px-3 py-3">
      <ol className="flex items-center gap-1 overflow-x-auto">
        {WORKFLOW_STEPS.map((step, i) => {
          const done = i < current;
          const active = i === current;
          const clickable = !!onJump && i <= reached;
          return (
            <li key={step.key} className="flex shrink-0 items-center">
              <button
                type="button"
                disabled={!clickable}
                onClick={() => clickable && onJump?.(i)}
                className={`flex items-center gap-2.5 rounded-2xl px-3 py-2 text-left transition-colors duration-200
                  ${clickable ? "cursor-pointer hover:bg-white" : "cursor-default"}
                  ${active ? "bg-white shadow-card" : ""}`}
              >
                <span
                  className={`flex h-7 w-7 items-center justify-center rounded-full text-xs font-semibold transition-colors
                    ${active
                      ? "bg-apple text-white"
                      : done
                        ? "bg-success.dot text-white"
                        : "bg-white text-faint ring-1 ring-line"
                    }`}
                >
                  {done ? (
                    <svg
                      className="h-3.5 w-3.5"
                      viewBox="0 0 20 20"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.4"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <path d="M4 10.5l4 4 8-9" />
                    </svg>
                  ) : (
                    i + 1
                  )}
                </span>
                <span className="leading-tight">
                  <span
                    className={`block text-sm font-medium ${
                      active ? "text-ink" : done ? "text-muted" : "text-faint"
                    }`}
                  >
                    {step.label}
                  </span>
                </span>
              </button>
              {i < WORKFLOW_STEPS.length - 1 && (
                <span
                  aria-hidden
                  className={`mx-1 h-px w-5 rounded-full ${
                    i < current ? "bg-success.dot/60" : "bg-line"
                  }`}
                />
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
