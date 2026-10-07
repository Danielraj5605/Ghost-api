import type {
  AppState,
  ClientEvent,
  DataFormat,
  NewWatch,
  Watch,
  WatchChange,
  WatchCheck,
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

/** A site ("www.google.com/maps") as one URL path segment ("www.google.com~maps"). */
export const siteId = (site: string) => encodeURIComponent(site.replace("/", "~"));
export const siteFromId = (id: string) => id.replace("~", "/");

/** URLs used directly by <img>, <a download> and EventSource (no custom headers). */
export const withToken = (path: string) =>
  `${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(token())}`;

export const api = {
  state: () => request<AppState>("/api/state"),
  sites: () => request<SiteSummary[]>("/api/sites"),
  site: (domain: string) => request<SiteDetail>(`/api/sites/${siteId(domain)}`),
  file: (domain: string, name: string) =>
    request<string>(`/api/sites/${siteId(domain)}/files/${encodeURIComponent(name)}`),
  startRun: (body: StartRunRequest) => post<{ run_id: number }>("/api/runs", body),
  stopRun: (id: number) => post<{ stopping: boolean }>(`/api/runs/${id}/stop`, {}),
  regenerate: (domain: string, lang: string) =>
    post<ClientEvent>(`/api/sites/${siteId(domain)}/generate`, { lang }),
  tryEndpoint: (domain: string, endpoint: string, params: Record<string, string>) =>
    post<TryResult>(`/api/sites/${siteId(domain)}/try`, { endpoint, params }),
  doctor: () => post<ProviderCheck[]>("/api/doctor", {}),
  watches: () => request<{ watches: Watch[]; changes: WatchChange[] }>("/api/watches"),
  addWatch: (body: NewWatch) => post<{ watch: Watch; check: WatchCheck }>("/api/watches", body),
  checkWatch: (id: number) => post<WatchCheck>(`/api/watches/${id}/check`, {}),
  removeWatch: (id: number) => request<{ removed: boolean }>(`/api/watches/${id}`, { method: "DELETE" }),
  allow: (domain: string, endpoint: string, allowed: boolean) =>
    post<{ allowed: boolean }>(`/api/sites/${siteId(domain)}/allow`, { endpoint, allowed }),
  /** All pages as a file; resolves when the browser has been handed the download. */
  fetchData: async (
    domain: string,
    endpoint: string,
    params: Record<string, string>,
    pages: number,
    format: DataFormat,
  ): Promise<{ rows: number; filename: string }> => {
    const response = await fetch(`/api/sites/${siteId(domain)}/fetch`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Ghost-Token": token() },
      body: JSON.stringify({ endpoint, params, pages, format }),
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
    const disposition = response.headers.get("content-disposition") ?? "";
    const filename = /filename="([^"]+)"/.exec(disposition)?.[1] ?? `data.${format}`;
    const url = URL.createObjectURL(await response.blob());
    const link = Object.assign(document.createElement("a"), { href: url, download: filename });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
    return { rows: Number(response.headers.get("x-ghost-rows") ?? 0), filename };
  },
  eventsUrl: (id: number) => withToken(`/api/runs/${id}/events`),
  screenUrl: (id: number, name: string) => withToken(`/api/runs/${id}/screens/${name}`),
  zipUrl: (domain: string) => withToken(`/api/sites/${siteId(domain)}/download.zip`),
};
