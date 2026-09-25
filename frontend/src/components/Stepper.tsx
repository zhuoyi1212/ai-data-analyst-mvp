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
    <nav className="flex items-center gap-1 overflow-x-auto pb-1">
      {WORKFLOW_STEPS.map((step, i) => {
        const done = i < current;
        const active = i === current;
        const clickable = onJump && i <= reached;
        return (
          <div key={step.key} className="flex shrink-0 items-center">
            <button
              type="button"
              disabled={!clickable}
              onClick={() => clickable && onJump(i)}
              className={`group flex items-center gap-2 rounded-lg px-3 py-2 text-left ${
                clickable ? "cursor-pointer hover:bg-zinc-100" : "cursor-default"
              }`}
            >
              <span
                className={`flex h-6 w-6 items-center justify-center rounded-full text-xs font-semibold ${
                  active
                    ? "bg-accent text-white"
                    : done
                      ? "bg-emerald-500 text-white"
                      : "bg-zinc-200 text-zinc-500"
                }`}
              >
                {done ? "✓" : i + 1}
              </span>
              <span className="leading-tight">
                <span
                  className={`block text-sm font-medium ${
                    active ? "text-zinc-900" : done ? "text-zinc-700" : "text-zinc-400"
                  }`}
                >
                  {step.label}
                </span>
                <span className="block text-xs text-zinc-400">{step.hint}</span>
              </span>
            </button>
            {i < WORKFLOW_STEPS.length - 1 && (
              <span className={`mx-1 h-px w-5 ${i < current ? "bg-emerald-400" : "bg-zinc-200"}`} />
            )}
          </div>
        );
      })}
    </nav>
  );
}
