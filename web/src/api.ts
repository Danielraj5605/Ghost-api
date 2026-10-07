import type {
  AppState,
  ClientEvent,
  ProviderCheck,
  SiteDetail,
  SiteSummary,
  StartRunRequest,
  TryResult,
} from "./types";

declare global {
  interface Window {
    GHOST_TOKEN?: string;
  }
}

/** The per-launch token the server put in the page (see ghost_api/web/server.py). */
export function token(): string {
  const fromPage = window.GHOST_TOKEN ?? "";
  if (fromPage && !fromPage.startsWith("__")) return fromPage;
  return (import.meta.env.VITE_GHOST_TOKEN as string | undefined) ?? "";
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", "X-Ghost-Token": token(), ...init.headers },
  });
  if (!response.ok) {
    let message = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, message);
  }
  const type = response.headers.get("content-type") ?? "";
  return (type.includes("application/json") ? response.json() : response.text()) as Promise<T>;
}

const post = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body) });

/** URLs used directly by <img>, <a download> and EventSource (no custom headers). */
export const withToken = (path: string) =>
  `${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(token())}`;

export const api = {
  state: () => request<AppState>("/api/state"),
  sites: () => request<SiteSummary[]>("/api/sites"),
  site: (domain: string) => request<SiteDetail>(`/api/sites/${encodeURIComponent(domain)}`),
  file: (domain: string, name: string) =>
    request<string>(`/api/sites/${encodeURIComponent(domain)}/files/${encodeURIComponent(name)}`),
  startRun: (body: StartRunRequest) => post<{ run_id: number }>("/api/runs", body),
  stopRun: (id: number) => post<{ stopping: boolean }>(`/api/runs/${id}/stop`, {}),
  regenerate: (domain: string, lang: string) =>
    post<ClientEvent>(`/api/sites/${encodeURIComponent(domain)}/generate`, { lang }),
  tryEndpoint: (domain: string, endpoint: string, params: Record<string, string>) =>
    post<TryResult>(`/api/sites/${encodeURIComponent(domain)}/try`, { endpoint, params }),
  doctor: () => post<ProviderCheck[]>("/api/doctor", {}),
  eventsUrl: (id: number) => withToken(`/api/runs/${id}/events`),
  screenUrl: (id: number, name: string) => withToken(`/api/runs/${id}/screens/${name}`),
  zipUrl: (domain: string) => withToken(`/api/sites/${encodeURIComponent(domain)}/download.zip`),
};
