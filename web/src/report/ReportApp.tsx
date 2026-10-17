import { Activity, BookOpen, Code2, FileText, Lightbulb, ListTree } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { CodeBlock, languageFor } from "../components/CodeBlock";
import { EndpointDetailPanel, EndpointList } from "../components/Endpoints";
import { ExposureBanner } from "../components/Exposure";
import { Logo } from "../components/Logo";
import { ThemeToggle } from "../components/ThemeToggle";
import { Timeline } from "../components/Timeline";
import { bytes, Card, cx, EmptyState, Pill, plural, Tabs } from "../components/ui";
import { initialRunState, runReducer } from "../state/runReducer";
import type { EndpointDetail, GhostEvent, RunInfo, SiteFile } from "../types";

/** The JSON that ghost_api/export/site_data.py:report_data embeds in the page. */
export interface ReportData {
  version: string;
  generated_at: string;
  domain: string;
  run: RunInfo;
  endpoints: EndpointDetail[];
  events: GhostEvent[];
  lessons: string[];
  files: (SiteFile & { content: string | null })[];
  report_md: string;
}

export function readReportData(doc: Document): ReportData | null {
  const text = doc.getElementById("ghost-report")?.textContent?.trim() ?? "";
  if (!text || text.startsWith("__")) return null; // the unfilled template
  try {
    return JSON.parse(text) as ReportData;
  } catch {
    return null;
  }
}

type TabId = "endpoints" | "activity" | "code" | "docs" | "report" | "lessons";
const TAB_IDS: TabId[] = ["endpoints", "activity", "code", "docs", "report", "lessons"];

/** The open tab lives in the URL hash (#code), so a link can point at it. */
function useHashTab(): [TabId, (t: TabId) => void] {
  const read = (): TabId => {
    const id = window.location.hash.slice(1) as TabId;
    return TAB_IDS.includes(id) ? id : "endpoints";
  };
  const [tab, setTab] = useState<TabId>(read);
  useEffect(() => {
    const onHash = () => setTab(read());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const change = (t: TabId) => {
    history.replaceState(null, "", t === "endpoints" ? " " : `#${t}`);
    setTab(t);
  };
  return [tab, change];
}

function seconds(run: RunInfo): number | null {
  if (!run.finished_at) return null;
  const s = (new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000;
  return Number.isFinite(s) ? Math.max(0, Math.round(s)) : null;
}

function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-xl border border-line bg-surface px-4 py-3">
      <div className="text-xs text-muted">{label}</div>
      <div className="mt-0.5 text-lg font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function EndpointsTab({ endpoints }: { endpoints: EndpointDetail[] }) {
  const first = endpoints.find((e) => !e.third_party) ?? endpoints[0];
  const [selected, setSelected] = useState<string | null>(first?.key ?? null);
  const endpoint = endpoints.find((e) => e.key === selected);
  if (!endpoints.length) {
    return (
      <EmptyState title="No endpoints were found">
        The site may build its pages on the server, so there was no hidden API to capture.
      </EmptyState>
    );
  }
  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-4 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
      <div className="lg:sticky lg:top-20 lg:max-h-[calc(100vh-7rem)] lg:overflow-y-auto lg:pr-1">
        <EndpointList endpoints={endpoints} selected={selected} onSelect={setSelected} />
      </div>
      <Card className="p-4 sm:p-5">{endpoint && <EndpointDetailPanel endpoint={endpoint} />}</Card>
    </div>
  );
}

function ActivityTab({ events }: { events: GhostEvent[] }) {
  const items = useMemo(
    () => events.reduce((s, event) => runReducer(s, { type: "event", event }), initialRunState).timeline,
    [events],
  );
  if (!items.length) return <EmptyState title="No activity was recorded for this run" />;
  return (
    <Card className="max-h-[75vh] overflow-y-auto">
      <Timeline items={items} follow={false} />
    </Card>
  );
}

function FilesTab({ files, empty }: { files: ReportData["files"]; empty: string }) {
  const [name, setName] = useState(files[0]?.name);
  const file = files.find((f) => f.name === name) ?? files[0];
  if (!file) return <EmptyState icon={<Code2 className="size-8" />} title={empty} />;
  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-3 lg:grid-cols-[14rem_minmax(0,1fr)]">
      <ul className="flex gap-1 overflow-x-auto lg:flex-col" aria-label="Files">
        {files.map((f) => (
          <li key={f.name} className="shrink-0">
            <button
              type="button"
              onClick={() => setName(f.name)}
              aria-current={f.name === file.name}
              className={cx(
                "flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-sm transition",
                f.name === file.name ? "bg-surface-2 text-fg" : "text-muted hover:text-fg",
              )}
            >
              <span className="min-w-0 flex-1 truncate font-mono text-xs">{f.name}</span>
              <span className="text-[11px] text-muted">{f.kind}</span>
            </button>
          </li>
        ))}
      </ul>
      <div className="min-w-0">
        {file.content === null ? (
          <Card>
            <EmptyState title={`${file.name} is too big to include (${bytes(file.size)})`}>
              It is in the output folder next to this report.
            </EmptyState>
          </Card>
        ) : (
          <CodeBlock code={file.content} language={languageFor(file.name)} title={file.name} maxHeight="75vh" />
        )}
      </div>
    </div>
  );
}

export function ReportApp({ data }: { data: ReportData | null }) {
  const [tab, setTab] = useHashTab();

  useEffect(() => {
    if (data) document.title = `${data.domain} · Ghost API report`;
  }, [data]);

  if (!data) {
    return (
      <EmptyState title="This report has no data">
        It is the empty report template. Ghost API fills it in when it writes report.html.
      </EmptyState>
    );
  }

  const { run } = data;
  const visible = data.endpoints.filter((e) => !e.third_party);
  const verified = visible.filter((e) => e.status === "verified").length;
  const code = data.files.filter((f) => f.kind !== "openapi");
  const specs = data.files.filter((f) => f.kind === "openapi");
  const duration = seconds(run);
  const failed = ["failed", "interrupted"].includes(run.status);

  return (
    <div className="flex min-h-screen flex-col">
      <header className="sticky top-0 z-20 border-b border-line bg-bg/80 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-[1440px] items-center gap-3 px-4">
          <Logo />
          <span className="text-sm text-muted">report</span>
          <div className="ml-auto">
            <ThemeToggle />
          </div>
        </div>
      </header>

      <main className="mx-auto w-full max-w-[1440px] flex-1 space-y-6 px-4 py-6">
        <section className="space-y-4 pt-2">
          <div className="flex flex-wrap items-start gap-3">
            <div className="min-w-0 flex-1">
              <h1 className="text-2xl font-bold break-all sm:text-3xl">{data.domain}</h1>
              <p className="mt-1 text-muted">Goal: {run.goal}</p>
              <p className="mt-1 truncate text-xs text-muted">
                Started at <span className="font-mono">{run.start_url}</span> · run #{run.id}
              </p>
            </div>
            <Pill className={failed ? "bg-warn/15 text-warn" : "bg-ok/15 text-ok"}>{run.status}</Pill>
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
            <Stat label="Endpoints" value={visible.length} />
            <Stat label="Verified by live tests" value={verified} />
            <Stat label="Requests captured" value={run.requests} />
            <Stat label="LLM calls" value={run.llm_calls} />
            <Stat label="Time" value={duration === null ? "n/a" : `${duration} s`} />
          </div>
        </section>

        <ExposureBanner endpoints={data.endpoints} />

        <Tabs
          value={tab}
          onChange={setTab}
          tabs={[
            { id: "endpoints", label: <Label icon={<ListTree className="size-3.5" />}>Endpoints</Label>, count: visible.length },
            { id: "activity", label: <Label icon={<Activity className="size-3.5" />}>Activity</Label> },
            { id: "code", label: <Label icon={<Code2 className="size-3.5" />}>Client code</Label>, count: code.length },
            { id: "docs", label: <Label icon={<BookOpen className="size-3.5" />}>API docs</Label> },
            { id: "report", label: <Label icon={<FileText className="size-3.5" />}>Summary</Label> },
            { id: "lessons", label: <Label icon={<Lightbulb className="size-3.5" />}>Lessons</Label>, count: data.lessons.length },
          ]}
        />

        {tab === "endpoints" && <EndpointsTab endpoints={data.endpoints} />}
        {tab === "activity" && <ActivityTab events={data.events} />}
        {tab === "code" && <FilesTab files={code} empty="No client was generated" />}
        {tab === "docs" && <FilesTab files={specs} empty="No OpenAPI spec" />}
        {tab === "report" && (
          <Card className="p-4 sm:p-6">
            <div className="prose-ghost max-w-none">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{data.report_md}</ReactMarkdown>
            </div>
          </Card>
        )}
        {tab === "lessons" &&
          (data.lessons.length ? (
            <Card>
              <ul className="divide-y divide-line">
                {data.lessons.map((l) => (
                  <li key={l} className="flex gap-3 px-4 py-3 text-sm">
                    <Lightbulb className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden />
                    {l}
                  </li>
                ))}
              </ul>
            </Card>
          ) : (
            <EmptyState title="No lessons saved for this site yet" />
          ))}
      </main>

      <footer className="border-t border-line py-4 text-center text-xs text-muted">
        Made with Ghost API {data.version} on {new Date(data.generated_at).toLocaleString()} ·{" "}
        {plural(data.endpoints.length, "endpoint")} · this page is one file and works offline
      </footer>
    </div>
  );
}

function Label({ icon, children }: { icon: ReactNode; children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      {icon}
      {children}
    </span>
  );
}
