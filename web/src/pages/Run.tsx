import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Clock, Coins, Cpu, Network, Square } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { EndpointList } from "../components/Endpoints";
import { LiveBrowser } from "../components/LiveBrowser";
import { PhaseStepper } from "../components/PhaseStepper";
import { Timeline } from "../components/Timeline";
import { Button, Card, cx, EmptyState, ErrorBox, Pill, plural, SectionTitle, Skeleton, Tabs } from "../components/ui";
import { useRunStream } from "../hooks/useRunStream";
import { stepStates, type RunState } from "../state/runReducer";
import type { Box } from "../types";

function Stat({ icon, label, value }: { icon: ReactNode; label: string; value: ReactNode }) {
  return (
    <div className="flex items-center gap-2 rounded-lg bg-surface-2/60 px-3 py-2">
      <span className="text-muted">{icon}</span>
      <div>
        <div className="text-[11px] text-muted">{label}</div>
        <div className="text-sm font-semibold tabular-nums">{value}</div>
      </div>
    </div>
  );
}

function Summary({ state }: { state: RunState }) {
  const f = state.finished;
  if (!f) return null;
  const good = !["failed", "interrupted"].includes(f.status);
  return (
    <Card className={cx("p-4", good ? "border-ok/40" : "border-warn/40")}>
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="font-semibold">
            {good ? "Done" : "Stopped"}: {plural(f.endpoints, "endpoint")} found in {Math.round(f.seconds)} s
          </h2>
          <p className="text-sm text-muted">
            {f.status}
            {f.reason ? ` · ${f.reason}` : ""}
            {f.lessons_learned ? ` · ${plural(f.lessons_learned, "lesson")} saved for next time` : ""}
          </p>
          {f.generation_error && <p className="mt-1 text-sm text-bad">{f.generation_error}</p>}
        </div>
        <Link to={`/sites/${encodeURIComponent(state.domain)}`}>
          <Button variant="primary">
            Open results <ArrowRight className="size-4" />
          </Button>
        </Link>
      </div>
    </Card>
  );
}

type MobileTab = "browser" | "timeline" | "endpoints";

export function Run() {
  const runId = Number(useParams().id);
  const { state, status } = useRunStream(Number.isFinite(runId) ? runId : null);
  const [picked, setPicked] = useState<{ name: string; box: Box | null } | null>(null);
  const [tab, setTab] = useState<MobileTab>("timeline");
  const qc = useQueryClient();
  const ended = state.finished !== null || status === "ended";
  useEffect(() => {
    // The header's "live" badge and the site list change when a run ends.
    if (ended) {
      void qc.invalidateQueries({ queryKey: ["state"] });
      void qc.invalidateQueries({ queryKey: ["sites"] });
      void qc.invalidateQueries({ queryKey: ["site", state.domain] });
    }
  }, [ended, status, qc, state.domain]);
  const stop = useMutation({ mutationFn: () => api.stopRun(runId) });

  const live = !ended;
  const shown = picked ?? state.screenshot;
  const usage = state.usage;

  if (!Number.isFinite(runId)) return <ErrorBox error="Not a run number." />;

  const browser = (
    <div className="space-y-3">
      <LiveBrowser runId={state.runId} shot={shown} url={state.startUrl} live={live} />
      {picked && (
        <Button variant="ghost" onClick={() => setPicked(null)} className="text-xs">
          Back to latest screenshot
        </Button>
      )}
      <div className="grid grid-cols-2 gap-2">
        <Stat icon={<Network className="size-4" />} label="Requests" value={state.requests} />
        <Stat icon={<Cpu className="size-4" />} label="LLM calls" value={usage?.answered ?? 0} />
        <Stat icon={<Coins className="size-4" />} label="Tokens · cost" value={`${(usage?.tokens ?? 0).toLocaleString()} · $0.00`} />
        <Stat
          icon={<Clock className="size-4" />}
          label="Direct calls"
          value={state.probes ? state.probes.directCalls : "—"}
        />
      </div>
      {usage && usage.problems.length > 0 && (
        <ul className="space-y-1 text-xs text-warn">
          {usage.problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
    </div>
  );

  const timeline = (
    <Card className="flex h-[32rem] flex-col lg:h-[calc(100vh-15rem)]">
      <SectionTitle right={<Pill>{plural(state.timeline.length, "event")}</Pill>}>Activity</SectionTitle>
      <div className="min-h-0 flex-1">
        {state.timeline.length ? (
          <Timeline items={state.timeline} selectedShot={picked?.name} onSelectShot={setPicked} />
        ) : status === "error" ? (
          <EmptyState title="Can't reach the run">Is `ghost-api ui` still running?</EmptyState>
        ) : (
          <div className="space-y-2 p-4">
            <Skeleton className="h-10" />
            <Skeleton className="h-10 w-4/5" />
            <Skeleton className="h-10 w-3/5" />
          </div>
        )}
      </div>
    </Card>
  );

  const endpoints = (
    <Card className="flex max-h-[32rem] flex-col lg:h-[calc(100vh-15rem)] lg:max-h-none">
      <SectionTitle right={<Pill>{state.endpoints.filter((e) => !e.third_party).length}</Pill>}>Endpoints</SectionTitle>
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        <EndpointList endpoints={state.endpoints} compact />
      </div>
    </Card>
  );

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-sm text-muted">
            <span>Run #{runId}</span>
            {live ? (
              <span className="flex items-center gap-1 text-accent">
                <span className="size-1.5 animate-pulse rounded-full bg-accent" /> running
              </span>
            ) : (
              <span>finished</span>
            )}
          </div>
          <h1 className="truncate text-xl font-bold">{state.domain || "Starting…"}</h1>
          {state.goal && <p className="truncate text-sm text-muted">Goal: {state.goal}</p>}
        </div>
        {live && state.runId !== null && (
          <Button variant="danger" onClick={() => stop.mutate()} disabled={stop.isPending || stop.isSuccess}>
            <Square className="size-3.5" />
            {stop.isSuccess ? "Stopping…" : "Stop"}
          </Button>
        )}
      </header>

      <PhaseStepper states={stepStates(state)} />
      {stop.error && <ErrorBox error={stop.error} />}
      <Summary state={state} />

      {/* Phones: one panel at a time. */}
      <div className="md:hidden">
        <Tabs
          className="mb-3"
          value={tab}
          onChange={setTab}
          tabs={[
            { id: "timeline", label: "Activity" },
            { id: "browser", label: "Browser" },
            { id: "endpoints", label: "Endpoints", count: state.endpoints.length },
          ]}
        />
        {tab === "browser" && browser}
        {tab === "timeline" && timeline}
        {tab === "endpoints" && endpoints}
      </div>

      {/* Tablets: two columns; desktops: three. */}
      <div className="hidden gap-4 md:grid md:grid-cols-2 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)_minmax(0,0.8fr)]">
        <div className="space-y-4">
          {browser}
          <div className="lg:hidden">{endpoints}</div>
        </div>
        {timeline}
        <div className="hidden lg:block">{endpoints}</div>
      </div>
    </div>
  );
}
