import { Check, Circle, Loader2, Minus } from "lucide-react";
import { STEPPER, type StepState } from "../state/runReducer";
import { cx } from "./ui";

const ICON: Record<StepState, typeof Check> = {
  done: Check,
  active: Loader2,
  todo: Circle,
  skipped: Minus,
};

export function PhaseStepper({ states }: { states: StepState[] }) {
  return (
    <ol className="flex flex-wrap items-center gap-x-1 gap-y-2" aria-label="Run progress">
      {STEPPER.map((step, i) => {
        const state = states[i] ?? "todo";
        const Icon = ICON[state];
        return (
          <li key={step.phase} className="flex items-center gap-1">
            {i > 0 && <span className={cx("h-px w-4 sm:w-6", state === "todo" ? "bg-line" : "bg-accent/50")} />}
            <span
              aria-current={state === "active" ? "step" : undefined}
              className={cx(
                "flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium",
                state === "done" && "bg-ok/15 text-ok",
                state === "active" && "bg-accent-soft text-accent",
                state === "todo" && "bg-surface-2 text-muted",
                state === "skipped" && "text-muted line-through decoration-muted/50",
              )}
            >
              <Icon className={cx("size-3.5", state === "active" && "animate-spin")} aria-hidden />
              {step.label}
              <span className="sr-only">({state})</span>
            </span>
          </li>
        );
      })}
    </ol>
  );
}
