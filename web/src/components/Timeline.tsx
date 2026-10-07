import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  ChevronRight,
  CircleHelp,
  Code2,
  Flag,
  FlaskConical,
  Globe,
  Info,
  Keyboard,
  MousePointerClick,
  MoveVertical,
  Plug,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { TimelineItem } from "../state/runReducer";
import type { Box, Level } from "../types";
import { cx, plural, Pill } from "./ui";

const ACTION_ICON: Record<string, typeof Globe> = {
  click: MousePointerClick,
  type: Keyboard,
  scroll: MoveVertical,
  navigate: Globe,
  go_back: ArrowLeft,
  call_endpoint: Plug,
  ask_user: CircleHelp,
  finish: Flag,
};

const LEVEL: Record<Level, { icon: typeof Info; className: string }> = {
  info: { icon: Info, className: "text-muted" },
  success: { icon: CheckCircle2, className: "text-ok" },
  warning: { icon: AlertTriangle, className: "text-warn" },
  error: { icon: XCircle, className: "text-bad" },
};

/** "click #12", "type 'laptop' ↵", "scroll down"... */
export function describeAction(action: string, args: Record<string, unknown>): string {
  const a = args;
  switch (action) {
    case "click":
      return `Click element #${String(a.element_id ?? "?")}`;
    case "type":
      return `Type "${String(a.text ?? "")}"${a.submit ? " and press Enter" : ""}`;
    case "scroll":
      return `Scroll ${String(a.direction ?? "down")}`;
    case "navigate":
      return `Open ${String(a.url ?? "")}`;
    case "go_back":
      return "Go back";
    case "call_endpoint":
      return `Call ${String(a.endpoint ?? "")}`;
    case "ask_user":
      return `Ask: ${String(a.question ?? "")}`;
    case "finish":
      return "Finish";
    default:
      return action;
  }
}

function Row({
  icon,
  iconClass,
  title,
  children,
  details,
  onSelect,
  selected,
}: {
  icon: ReactNode;
  iconClass?: string;
  title: ReactNode;
  children?: ReactNode;
  details?: ReactNode;
  onSelect?: () => void;
  selected?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <li
      className={cx(
        "relative flex gap-3 rounded-lg px-2 py-2 transition",
        selected && "bg-accent-soft/60",
        onSelect && "cursor-pointer hover:bg-surface-2",
      )}
      onClick={onSelect}
    >
      <span className={cx("mt-0.5 shrink-0", iconClass ?? "text-accent")}>{icon}</span>
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium break-words">{title}</div>
        {children && <div className="mt-0.5 text-sm text-muted break-words">{children}</div>}
        {details && (
          <>
            <button
              type="button"
              onClick={(ev) => {
                ev.stopPropagation();
                setOpen(!open);
              }}
              aria-expanded={open}
              className="mt-1 inline-flex items-center gap-0.5 text-xs text-muted hover:text-fg"
            >
              <ChevronRight className={cx("size-3 transition", open && "rotate-90")} />
              {open ? "Hide details" : "Details"}
            </button>
            {open && <div className="mt-1 text-xs text-muted">{details}</div>}
          </>
        )}
      </div>
    </li>
  );
}

export function Timeline({
  items,
  selectedShot,
  onSelectShot,
  follow = true,
}: {
  items: TimelineItem[];
  selectedShot?: string | null;
  onSelectShot?: (shot: { name: string; box: Box | null }) => void;
  follow?: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);

  // Keep the newest line in view, unless the reader scrolled up.
  useEffect(() => {
    const el = box.current;
    if (!follow || !el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
    // Scroll the box itself: scrollIntoView would also scroll the page.
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [items.length, follow]);

  return (
    <div ref={box} className="h-full overflow-y-auto px-2 py-2">
      <ol aria-live="polite" aria-relevant="additions" className="space-y-0.5">
        {items.map((item) => {
          switch (item.kind) {
            case "notice": {
              const { icon: Icon, className } = LEVEL[item.level];
              return (
                <Row
                  key={item.id}
                  icon={<Icon className="size-4" />}
                  iconClass={className}
                  title={<span className={item.level === "info" ? "font-normal" : ""}>{item.text}</span>}
                  details={item.detail ? <pre className="font-mono whitespace-pre-wrap">{item.detail}</pre> : undefined}
                />
              );
            }
            case "step": {
              const Icon = ACTION_ICON[item.action] ?? Globe;
              const shot = item.screenshot;
              return (
                <Row
                  key={item.id}
                  icon={<Icon className="size-4" />}
                  title={
                    <span className="flex flex-wrap items-center gap-x-2">
                      <span className="text-xs font-semibold text-muted">Step {item.step}</span>
                      {describeAction(item.action, item.args)}
                    </span>
                  }
                  selected={!!shot && shot === selectedShot}
                  onSelect={shot && onSelectShot ? () => onSelectShot({ name: shot, box: item.box }) : undefined}
                  details={
                    item.result ? <pre className="font-mono whitespace-pre-wrap">{item.result}</pre> : undefined
                  }
                >
                  {item.thought && <p className="italic">“{item.thought}”</p>}
                  <p className="mt-1 flex flex-wrap items-center gap-1.5 text-xs">
                    <span>{plural(item.requests, "new request")}</span>
                    {item.newEndpoints.map((ep) => (
                      <Pill key={ep} className="bg-accent-soft text-accent">
                        + {ep}
                      </Pill>
                    ))}
                  </p>
                </Row>
              );
            }
            case "hypothesis": {
              const ok = item.result === "confirmed";
              const bad = item.result === "rejected";
              return (
                <Row
                  key={item.id}
                  icon={<FlaskConical className="size-4" />}
                  iconClass={ok ? "text-ok" : bad ? "text-bad" : "text-warn"}
                  title={
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-xs">{item.endpoint}</span>
                      <Pill className={ok ? "bg-ok/15 text-ok" : bad ? "bg-bad/15 text-bad" : "bg-warn/15 text-warn"}>
                        {item.result}
                      </Pill>
                    </span>
                  }
                  details={item.evidence ? <span>{item.evidence}</span> : undefined}
                >
                  {item.text}
                </Row>
              );
            }
            case "round": {
              const r = item.round;
              const clean = r.failed === 0;
              return (
                <Row
                  key={item.id}
                  icon={<ShieldCheck className="size-4" />}
                  iconClass={clean ? "text-ok" : "text-warn"}
                  title={`Verification round ${r.number}: ${r.passed} passed, ${r.failed} failed${r.skipped ? `, ${r.skipped} skipped` : ""}`}
                  details={
                    r.failures.length || r.repairs.length ? (
                      <ul className="space-y-1">
                        {r.failures.map((f) => (
                          <li key={f.name}>
                            <span className="font-mono text-bad">{f.name}</span>: {f.message}
                          </li>
                        ))}
                        {r.repairs.map((f) => (
                          <li key={`${f.method}-${f.kind}`}>
                            Repaired <span className="font-mono">{f.method}</span> ({f.kind}): {f.action}
                          </li>
                        ))}
                      </ul>
                    ) : undefined
                  }
                />
              );
            }
            case "client": {
              const c = item.client;
              return (
                <Row
                  key={item.id}
                  icon={<Code2 className="size-4" />}
                  iconClass="text-info"
                  title={
                    <span>
                      Generated <span className="font-mono">{c.class_name}</span> ({c.lang}) with{" "}
                      {plural(c.methods.length, "method")}
                    </span>
                  }
                >
                  {c.methods
                    .slice(0, 6)
                    .map((m) => m.name)
                    .join(", ")}
                  {c.methods.length > 6 ? "…" : ""}
                </Row>
              );
            }
          }
        })}
      </ol>
    </div>
  );
}
