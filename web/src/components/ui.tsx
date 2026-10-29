import type { ButtonHTMLAttributes, ReactNode } from "react";
import type { Confidence, EndpointStatus } from "../types";

export function cx(...names: (string | false | null | undefined)[]): string {
  return names.filter(Boolean).join(" ");
}

// --- badges (same colors as the terminal: ghost_api/ui/terminal.py) ---

const METHOD_CLASS: Record<string, string> = {
  GET: "bg-info/15 text-info",
  POST: "bg-ok/15 text-ok",
  PUT: "bg-warn/15 text-warn",
  PATCH: "bg-warn/15 text-warn",
  DELETE: "bg-bad/15 text-bad",
};

export function MethodBadge({ method }: { method: string }) {
  return (
    <span
      className={cx(
        "inline-flex min-w-12 justify-center rounded-md px-1.5 py-0.5 font-mono text-[11px] font-semibold",
        METHOD_CLASS[method] ?? "bg-surface-2 text-muted",
      )}
    >
      {method}
    </span>
  );
}

const STATUS: Record<EndpointStatus, { label: string; className: string }> = {
  verified: { label: "Verified", className: "bg-ok/15 text-ok" },
  unverified: { label: "Unverified", className: "bg-warn/15 text-warn" },
  failed: { label: "Failed", className: "bg-bad/15 text-bad" },
  discovered: { label: "Discovered", className: "bg-surface-2 text-muted" },
};

export function StatusBadge({ status, title }: { status: EndpointStatus; title?: string }) {
  const s = STATUS[status] ?? STATUS.discovered;
  return <Pill className={s.className} title={title}>{s.label}</Pill>;
}

const CONFIDENCE: Record<Confidence, string> = {
  High: "bg-ok/15 text-ok",
  Medium: "bg-warn/15 text-warn",
  Low: "bg-bad/15 text-bad",
};

export function ConfidenceBadge({ confidence }: { confidence: Confidence }) {
  return <Pill className={CONFIDENCE[confidence]}>{confidence} confidence</Pill>;
}

export function Pill({ children, className, title }: { children: ReactNode; className?: string; title?: string }) {
  return (
    <span
      title={title}
      className={cx("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap", className ?? "bg-surface-2 text-muted")}
    >
      {children}
    </span>
  );
}

// --- layout pieces ---

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("rounded-2xl border border-line bg-surface shadow-[var(--shadow-card)]", className)}>{children}</div>;
}

export function SectionTitle({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-2 border-b border-line px-5 py-3.5">
      <h2 className="text-sm font-semibold">{children}</h2>
      {right}
    </div>
  );
}

type Variant = "primary" | "secondary" | "ghost" | "danger";

const VARIANT: Record<Variant, string> = {
  primary: "bg-accent text-white hover:brightness-110 dark:text-[#0a0f20]",
  secondary: "border border-line bg-surface hover:bg-surface-2",
  ghost: "hover:bg-surface-2 text-muted hover:text-fg",
  danger: "border border-bad/40 text-bad hover:bg-bad/10",
};

export function Button({
  variant = "secondary",
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type="button"
      {...props}
      className={cx(
        "inline-flex items-center justify-center gap-1.5 rounded-xl px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50",
        VARIANT[variant],
        className,
      )}
    />
  );
}

/** Summary tile: a big number with a label. `filled` makes it the royal blue lead tile. */
export function StatTile({ label, value, note, filled }: { label: string; value: ReactNode; note?: ReactNode; filled?: boolean }) {
  return (
    <div
      className={cx(
        "rounded-2xl border p-4",
        filled
          ? "border-accent bg-accent text-white dark:text-[#0a0f20]"
          : "border-line bg-surface text-fg shadow-[var(--shadow-card)]",
      )}
    >
      <p className={cx("text-sm", filled ? "opacity-90" : "text-muted")}>{label}</p>
      <p className="mt-2 text-4xl font-semibold tabular-nums tracking-tight">{value}</p>
      {note && <p className={cx("mt-1 text-xs", filled ? "opacity-90" : "text-muted")}>{note}</p>}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cx("animate-pulse rounded-lg bg-surface-2", className)} aria-hidden />;
}

export function EmptyState({ icon, title, children }: { icon?: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-12 text-center">
      {icon && <div className="text-muted">{icon}</div>}
      <p className="font-medium">{title}</p>
      {children && <div className="max-w-md text-sm text-muted">{children}</div>}
    </div>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div role="alert" className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-bad">
      {message}
    </div>
  );
}

// --- tabs ---

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  className,
}: {
  tabs: { id: T; label: ReactNode; count?: number }[];
  value: T;
  onChange: (id: T) => void;
  className?: string;
}) {
  return (
    <div role="tablist" className={cx("flex gap-1 overflow-x-auto border-b border-line", className)}>
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          type="button"
          aria-selected={value === t.id}
          onClick={() => onChange(t.id)}
          className={cx(
            "-mb-px flex shrink-0 items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium transition",
            value === t.id ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg",
          )}
        >
          {t.label}
          {t.count !== undefined && (
            <span className="rounded-full bg-surface-2 px-1.5 text-[11px] text-muted">{t.count}</span>
          )}
        </button>
      ))}
    </div>
  );
}

// --- formatting ---

export const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "";
  const then = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`).getTime();
  if (Number.isNaN(then)) return iso;
  const s = Math.max(0, (Date.now() - then) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  return n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** Text from the agent, where `backticks` mark code (the LLM writes Markdown-ish). */
export function AgentText({ text }: { text: string }) {
  const parts = text.split(/`([^`\n]+)`/);
  return (
    <>
      {parts.map((part, i) =>
        i % 2 ? (
          <code key={i} className="rounded bg-surface-2 px-1 py-px font-mono text-[0.85em] not-italic">
            {part}
          </code>
        ) : (
          part
        ),
      )}
    </>
  );
}
