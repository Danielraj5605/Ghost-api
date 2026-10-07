import type {
  Box,
  ClientEvent,
  EndpointSummary,
  FinishedEvent,
  GhostEvent,
  Level,
  Outcome,
  Phase,
  RoundEvent,
  UsageEvent,
} from "../types";

/** One line in the run timeline. */
export type TimelineItem =
  | { id: number; kind: "notice"; text: string; level: Level; detail: string }
  | {
      id: number;
      kind: "step";
      step: number;
      action: string;
      args: Record<string, unknown>;
      thought: string;
      result: string;
      newEndpoints: string[];
      requests: number;
      screenshot: string | null;
      box: Box | null;
    }
  | { id: number; kind: "hypothesis"; endpoint: string; text: string; result: Outcome; evidence: string }
  | { id: number; kind: "round"; round: RoundEvent }
  | { id: number; kind: "client"; client: ClientEvent };

export interface RunState {
  runId: number | null;
  domain: string;
  startUrl: string;
  goal: string;
  mode: string;
  lang: string;
  phase: Phase;
  /** Phases reached so far, in order (for the stepper). */
  phasesSeen: Phase[];
  timeline: TimelineItem[];
  endpoints: EndpointSummary[];
  requests: number;
  thirdParty: number;
  screenshot: { name: string; box: Box | null } | null;
  usage: UsageEvent | null;
  client: ClientEvent | null;
  rounds: RoundEvent[];
  probes: { endpoints: number; directCalls: number } | null;
  finished: FinishedEvent | null;
  lastSeq: number;
}

export const initialRunState: RunState = {
  runId: null,
  domain: "",
  startUrl: "",
  goal: "",
  mode: "",
  lang: "python",
  phase: "checks",
  phasesSeen: [],
  timeline: [],
  endpoints: [],
  requests: 0,
  thirdParty: 0,
  screenshot: null,
  usage: null,
  client: null,
  rounds: [],
  probes: null,
  finished: null,
  lastSeq: 0,
};

export type RunAction = { type: "event"; event: GhostEvent } | { type: "reset" };

export function runReducer(state: RunState, action: RunAction): RunState {
  if (action.type === "reset") return initialRunState;
  const e = action.event;
  // Events are replayed after a reconnect; ignore any we already applied.
  if (e.seq && e.seq <= state.lastSeq) return state;
  const next = { ...state, lastSeq: e.seq || state.lastSeq };
  const add = (item: TimelineItem): RunState => ({ ...next, timeline: [...next.timeline, item] });

  switch (e.type) {
    case "run_started":
      return {
        ...next,
        runId: e.run_id,
        domain: e.domain,
        startUrl: e.start_url,
        goal: e.goal,
        mode: e.mode,
        lang: e.lang,
      };
    case "phase":
      return {
        ...next,
        phase: e.phase,
        phasesSeen: next.phasesSeen.includes(e.phase) ? next.phasesSeen : [...next.phasesSeen, e.phase],
      };
    case "notice":
      return add({ id: e.seq, kind: "notice", text: e.text, level: e.level, detail: e.detail });
    case "request":
      return { ...next, requests: next.requests + 1, thirdParty: next.thirdParty + (e.third_party ? 1 : 0) };
    case "step": {
      const withStep = add({
        id: e.seq,
        kind: "step",
        step: e.step,
        action: e.action,
        args: e.args,
        thought: e.thought,
        result: e.result,
        newEndpoints: e.new_endpoints,
        requests: e.requests,
        screenshot: e.screenshot,
        box: e.box,
      });
      return e.screenshot ? { ...withStep, screenshot: { name: e.screenshot, box: e.box } } : withStep;
    }
    case "screenshot":
      return { ...next, screenshot: { name: e.name, box: null } };
    case "endpoints":
      return { ...next, endpoints: e.endpoints };
    case "hypothesis":
      return add({
        id: e.seq,
        kind: "hypothesis",
        endpoint: e.endpoint,
        text: e.text,
        result: e.result,
        evidence: e.evidence,
      });
    case "probe_done":
      return { ...next, probes: { endpoints: e.endpoints, directCalls: e.direct_calls } };
    case "verify_round":
      return { ...add({ id: e.seq, kind: "round", round: e }), rounds: [...next.rounds, e] };
    case "client":
      return { ...add({ id: e.seq, kind: "client", client: e }), client: e };
    case "usage":
      return { ...next, usage: e };
    case "run_finished":
      return {
        ...next,
        finished: e,
        phase: "done",
        phasesSeen: next.phasesSeen.includes("done") ? next.phasesSeen : [...next.phasesSeen, "done"],
      };
    default:
      return next;
  }
}

/** The steps shown in the stepper; "checks" and "done" are implied. */
export const STEPPER: { phase: Phase; label: string }[] = [
  { phase: "explore", label: "Explore" },
  { phase: "test", label: "Test" },
  { phase: "generate", label: "Generate" },
  { phase: "verify", label: "Verify" },
  { phase: "learn", label: "Learn" },
];

export type StepState = "done" | "active" | "todo" | "skipped";

export function stepStates(state: RunState): StepState[] {
  const order: Phase[] = ["checks", ...STEPPER.map((s) => s.phase), "done"];
  const current = order.indexOf(state.phase);
  return STEPPER.map(({ phase }) => {
    const index = order.indexOf(phase);
    if (state.finished) return state.phasesSeen.includes(phase) ? "done" : "skipped";
    if (index < current) return state.phasesSeen.includes(phase) ? "done" : "skipped";
    if (index === current) return "active";
    return "todo";
  });
}
