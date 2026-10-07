// Shapes of the server's JSON. Events mirror ghost_api/events.py.

export type Phase = "checks" | "explore" | "test" | "generate" | "verify" | "learn" | "done";
export type Level = "info" | "success" | "warning" | "error";
export type Outcome = "confirmed" | "rejected" | "inconclusive";
export type EndpointStatus = "discovered" | "verified" | "unverified" | "failed";
export type Confidence = "High" | "Medium" | "Low";

export interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
  viewport_width: number;
  viewport_height: number;
}

export interface Param {
  name: string;
  type: string;
  location: "path" | "query";
  kind: string;
  examples: string[];
  sensitive: boolean;
}

export interface Pagination {
  style: "page" | "offset" | "cursor";
  param: string;
  location: string;
  size_param: string | null;
  confirmed: boolean;
}

export interface EndpointSummary {
  key: string;
  label: string;
  method: string;
  host: string;
  path: string;
  third_party: boolean;
  params: Param[];
  samples: number;
  statuses: number[];
  status: EndpointStatus;
  status_note: string;
  replayable: boolean | null;
  auth_type: string | null;
  required_headers: string[];
  pagination: Pagination | null;
  confidence: Confidence;
  fields: string;
  field_paths: string[];
  has_request_body: boolean;
}

export interface EndpointDetail extends EndpointSummary {
  request_schema: JsonSchema | null;
  response_schema: JsonSchema | null;
  example_urls: string[];
}

export interface JsonSchema {
  type?: string | string[];
  properties?: Record<string, JsonSchema>;
  items?: JsonSchema;
  required?: string[];
  anyOf?: JsonSchema[];
}

export interface ClientMethod {
  name: string;
  args: string[];
  returns: string;
  iterator: string | null;
  summary: string;
  endpoint: string;
}

export type GhostEvent =
  | { type: "run_started"; seq: number; run_id: number; domain: string; start_url: string; goal: string; mode: string; lang: string }
  | { type: "phase"; seq: number; phase: Phase }
  | { type: "notice"; seq: number; text: string; level: Level; detail: string }
  | { type: "request"; seq: number; method: string; url: string; status: number; third_party: boolean }
  | {
      type: "step";
      seq: number;
      step: number;
      action: string;
      args: Record<string, unknown>;
      thought: string;
      result: string;
      new_endpoints: string[];
      requests: number;
      screenshot: string | null;
      box: Box | null;
    }
  | { type: "screenshot"; seq: number; name: string; url: string }
  | { type: "endpoints"; seq: number; endpoints: EndpointSummary[] }
  | { type: "hypothesis"; seq: number; endpoint: string; text: string; result: Outcome; evidence: string }
  | { type: "probe_done"; seq: number; endpoints: number; direct_calls: number }
  | {
      type: "verify_round";
      seq: number;
      number: number;
      passed: number;
      failed: number;
      skipped: number;
      failures: { name: string; message: string }[];
      repairs: { method: string; kind: string; action: string }[];
    }
  | {
      type: "client";
      seq: number;
      class_name: string;
      lang: string;
      named_by_llm: boolean;
      directory: string;
      methods: ClientMethod[];
      credentials: { arg: string; env: string }[];
      skipped: string[];
      files: string[];
    }
  | {
      type: "usage";
      seq: number;
      answered: number;
      tokens: number;
      cache_hits: number;
      retries: number;
      fallbacks: number;
      by_provider: Record<string, number>;
      problems: string[];
    }
  | {
      type: "run_finished";
      seq: number;
      status: string;
      reason: string;
      seconds: number;
      requests: number;
      endpoints: number;
      report: string;
      openapi: string;
      lessons_learned: number;
      filtered: Record<string, number>;
      generation_error: string;
    };

export type ClientEvent = Extract<GhostEvent, { type: "client" }>;
export type UsageEvent = Extract<GhostEvent, { type: "usage" }>;
export type FinishedEvent = Extract<GhostEvent, { type: "run_finished" }>;
export type StepEvent = Extract<GhostEvent, { type: "step" }>;
export type RoundEvent = Extract<GhostEvent, { type: "verify_round" }>;

export interface RunInfo {
  id: number;
  domain: string;
  start_url: string;
  goal: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  requests: number;
  llm_calls: number;
  tokens: number;
}

export interface SiteSummary {
  domain: string;
  latest: RunInfo;
  runs: number;
  endpoints: number;
  counts: Record<EndpointStatus, number>;
  methods: string[];
  has_client: boolean;
}

export interface SiteFile {
  name: string;
  size: number;
  kind: "client" | "models" | "tests" | "openapi" | "report" | "other";
}

export interface SiteDetail {
  domain: string;
  runs: RunInfo[];
  endpoints: EndpointDetail[];
  lessons: string[];
  files: SiteFile[];
}

export interface AppState {
  version: string;
  running: number | null;
  has_llm: boolean;
  languages: string[];
  db: string;
  out: string;
}

export interface TryResult {
  status: number;
  ok: boolean;
  error: string | null;
  url: string;
  ms: number;
  body: string;
  truncated: boolean;
}

export interface ProviderCheck {
  provider: string;
  model: string;
  key: boolean;
  ok: boolean;
  detail: string;
  seconds: number;
}

export interface StartRunRequest {
  url: string;
  goal: string;
  lang: string;
  agent: boolean;
  max_steps: number;
  max_llm_calls: number;
  ignore_robots: boolean;
}
