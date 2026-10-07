import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, BookOpen, Download, FileCode2, History, Lightbulb, Play, RefreshCw, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import remarkGfm from "remark-gfm";
import { api } from "../api";
import { CodeBlock, languageFor } from "../components/CodeBlock";
import { EndpointDetailPanel, EndpointList } from "../components/Endpoints";
import {
  bytes,
  Button,
  Card,
  cx,
  EmptyState,
  ErrorBox,
  Pill,
  plural,
  Skeleton,
  Tabs,
  timeAgo,
} from "../components/ui";
import type { SiteDetail, SiteFile } from "../types";

type TabId = "endpoints" | "code" | "docs" | "report" | "runs" | "lessons";

function useFile(domain: string, name: string | undefined) {
  return useQuery({
    queryKey: ["file", domain, name],
    queryFn: () => api.file(domain, name ?? ""),
    enabled: !!name,
  });
}

function EndpointsTab({ site }: { site: SiteDetail }) {
  const visible = site.endpoints.filter((e) => !e.third_party);
  const [selected, setSelected] = useState<string | null>(visible[0]?.key ?? site.endpoints[0]?.key ?? null);
  const endpoint = site.endpoints.find((e) => e.key === selected);
  const detailRef = useRef<HTMLDivElement>(null);

  if (!site.endpoints.length) {
    return (
      <EmptyState title="No endpoints found for this site">
        The site may render everything on the server, so there was no hidden API to capture. The report tab
        explains what was seen.
      </EmptyState>
    );
  }
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
      <div className="lg:sticky lg:top-20 lg:max-h-[calc(100vh-7rem)] lg:overflow-y-auto lg:pr-1">
        <EndpointList
          endpoints={site.endpoints}
          selected={selected}
          onSelect={(key) => {
            setSelected(key);
            // On small screens the detail is below the list: bring it into view.
            if (window.innerWidth < 1024) detailRef.current?.scrollIntoView({ behavior: "smooth" });
          }}
        />
      </div>
      <div ref={detailRef} className="scroll-mt-20">
        <Card className="p-4 sm:p-5">
          {endpoint ? (
            <EndpointDetailPanel domain={site.domain} endpoint={endpoint} />
          ) : (
            <EmptyState title="Pick an endpoint" />
          )}
        </Card>
      </div>
    </div>
  );
}

const CODE_KINDS: SiteFile["kind"][] = ["client", "models", "tests", "other"];
const KIND_ORDER = { client: 0, models: 1, tests: 2, other: 3, openapi: 4, report: 5 };

function CodeTab({ site, languages }: { site: SiteDetail; languages: string[] }) {
  const qc = useQueryClient();
  const files = site.files
    .filter((f) => CODE_KINDS.includes(f.kind) && /\.(py|ts)$/.test(f.name))
    .sort((a, b) => KIND_ORDER[a.kind] - KIND_ORDER[b.kind] || a.name.localeCompare(b.name));
  const byLang = {
    python: files.filter((f) => f.name.endsWith(".py")),
    typescript: files.filter((f) => f.name.endsWith(".ts")),
  };
  const [lang, setLang] = useState<"python" | "typescript">(byLang.python.length || !byLang.typescript.length ? "python" : "typescript");
  const shown = byLang[lang];
  const [name, setName] = useState<string | undefined>(shown[0]?.name);
  useEffect(() => {
    if (!shown.some((f) => f.name === name)) setName(shown[0]?.name);
  }, [shown, name]);
  const file = useFile(site.domain, name);

  const regen = useMutation({
    mutationFn: () => api.regenerate(site.domain, lang),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["site", site.domain] });
      void qc.invalidateQueries({ queryKey: ["file", site.domain] });
    },
  });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex rounded-lg border border-line p-0.5" role="group" aria-label="Language">
          {(["python", "typescript"] as const)
            .filter((l) => languages.includes(l))
            .map((l) => (
              <button
                key={l}
                type="button"
                aria-pressed={lang === l}
                onClick={() => setLang(l)}
                className={cx(
                  "rounded-md px-3 py-1 text-sm font-medium transition",
                  lang === l ? "bg-accent-soft text-accent" : "text-muted hover:text-fg",
                )}
              >
                {l === "python" ? "Python" : "TypeScript"}
              </button>
            ))}
        </div>
        <Button onClick={() => regen.mutate()} disabled={regen.isPending || !site.endpoints.length}>
          {shown.length ? <RefreshCw className={cx("size-4", regen.isPending && "animate-spin")} /> : <Sparkles className="size-4" />}
          {regen.isPending ? "Generating…" : shown.length ? "Regenerate" : `Generate ${lang === "python" ? "Python" : "TypeScript"} client`}
        </Button>
        <a href={api.zipUrl(site.domain)} download className="ml-auto">
          <Button>
            <Download className="size-4" /> Download all (.zip)
          </Button>
        </a>
      </div>
      {regen.error && <ErrorBox error={regen.error} />}
      {regen.data && (
        <p className="text-sm text-ok" aria-live="polite">
          Generated {regen.data.class_name} with {plural(regen.data.methods.length, "method")}.
        </p>
      )}

      {shown.length === 0 ? (
        <Card>
          <EmptyState icon={<FileCode2 className="size-8" />} title={`No ${lang === "python" ? "Python" : "TypeScript"} client yet`}>
            {site.endpoints.length ? "Generate one with the button above. It takes a second and needs no LLM call." : "There are no endpoints to build a client from."}
          </EmptyState>
        </Card>
      ) : (
        <div className="grid gap-3 lg:grid-cols-[14rem_minmax(0,1fr)]">
          <ul className="flex gap-1 overflow-x-auto lg:flex-col" aria-label="Files">
            {shown.map((f) => (
              <li key={f.name} className="shrink-0">
                <button
                  type="button"
                  onClick={() => setName(f.name)}
                  aria-current={f.name === name}
                  className={cx(
                    "flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-sm transition",
                    f.name === name ? "bg-surface-2 text-fg" : "text-muted hover:text-fg",
                  )}
                >
                  <FileCode2 className="size-4 shrink-0" />
                  <span className="min-w-0 flex-1 truncate font-mono text-xs">{f.name}</span>
                  <span className="hidden text-[11px] text-muted lg:inline">{f.kind}</span>
                </button>
              </li>
            ))}
          </ul>
          <div className="min-w-0">
            {file.isPending && <Skeleton className="h-96" />}
            {file.error && <ErrorBox error={file.error} />}
            {file.data !== undefined && name && (
              <CodeBlock code={file.data} language={languageFor(name)} title={name} maxHeight="70vh" />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function DocsTab({ site }: { site: SiteDetail }) {
  const spec = site.files.find((f) => f.kind === "openapi");
  const file = useFile(site.domain, spec?.name);
  if (!spec) return <EmptyState icon={<BookOpen className="size-8" />} title="No OpenAPI spec yet" />;
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted">
        An OpenAPI 3.1 description of the endpoints. Import it into Postman, Insomnia or Swagger UI, or use it to
        generate clients in other languages.
      </p>
      {file.isPending && <Skeleton className="h-96" />}
      {file.error && <ErrorBox error={file.error} />}
      {file.data !== undefined && <CodeBlock code={file.data} language="yaml" title={spec.name} maxHeight="70vh" />}
    </div>
  );
}

function ReportTab({ site }: { site: SiteDetail }) {
  const report = site.files.find((f) => f.name === "report.md");
  const file = useFile(site.domain, report?.name);
  if (!report) return <EmptyState title="No report yet" />;
  return (
    <Card className="p-4 sm:p-6">
      {file.isPending && <Skeleton className="h-96" />}
      {file.error && <ErrorBox error={file.error} />}
      {file.data !== undefined && (
        <div className="prose-ghost max-w-none">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{file.data}</ReactMarkdown>
        </div>
      )}
    </Card>
  );
}

function RunsTab({ site }: { site: SiteDetail }) {
  return (
    <Card>
      <ul className="divide-y divide-line">
        {site.runs.map((r) => (
          <li key={r.id}>
            <Link to={`/runs/${r.id}`} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 transition hover:bg-surface-2/60">
              <span className="font-mono text-sm text-muted">#{r.id}</span>
              <span className="min-w-0 flex-1 truncate text-sm">{r.goal}</span>
              <Pill className={r.status === "failed" ? "bg-bad/15 text-bad" : r.status === "interrupted" ? "bg-warn/15 text-warn" : undefined}>
                {r.status}
              </Pill>
              <span className="text-xs text-muted">
                {plural(r.requests, "request")} · {plural(r.llm_calls, "LLM call")}
              </span>
              <span className="w-20 text-right text-xs text-muted">{timeAgo(r.started_at)}</span>
              <Play className="size-4 text-muted" aria-label="Replay" />
            </Link>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function LessonsTab({ site }: { site: SiteDetail }) {
  if (!site.lessons.length) {
    return (
      <EmptyState icon={<Lightbulb className="size-8" />} title="No lessons yet">
        After a run, Ghost API saves what worked on this site (useful pages, endpoints, dead ends) so the next
        refresh needs fewer LLM calls.
      </EmptyState>
    );
  }
  return (
    <Card>
      <ul className="divide-y divide-line">
        {site.lessons.map((l) => (
          <li key={l} className="flex gap-3 px-4 py-3 text-sm">
            <Lightbulb className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden />
            {l}
          </li>
        ))}
      </ul>
    </Card>
  );
}

const TAB_IDS: TabId[] = ["endpoints", "code", "docs", "report", "runs", "lessons"];

export function Site() {
  const domain = useParams().domain ?? "";
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const tabParam = params.get("tab") as TabId | null;
  const tab: TabId = tabParam && TAB_IDS.includes(tabParam) ? tabParam : "endpoints";
  const site = useQuery({ queryKey: ["site", domain], queryFn: () => api.site(domain) });
  const state = useQuery({ queryKey: ["state"], queryFn: api.state });

  const startAgain = useMutation({
    mutationFn: () => {
      const last = site.data?.runs[0];
      return api.startRun({
        url: last?.start_url ?? `https://${domain}/`,
        goal: last?.goal ?? "",
        lang: "python",
        agent: state.data?.has_llm ?? false,
        max_steps: 20,
        max_llm_calls: 30,
        ignore_robots: false,
      });
    },
    onSuccess: ({ run_id }) => navigate(`/runs/${run_id}`),
  });

  if (site.isPending) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-72" />
        <Skeleton className="h-10" />
        <Skeleton className="h-96" />
      </div>
    );
  }
  if (site.error) return <ErrorBox error={site.error} />;
  const s = site.data;
  const visible = s.endpoints.filter((e) => !e.third_party);
  const verified = visible.filter((e) => e.status === "verified").length;
  const last = s.runs[0];

  return (
    <div className="space-y-5">
      <Link to="/" className="inline-flex items-center gap-1 text-sm text-muted hover:text-fg">
        <ArrowLeft className="size-4" /> All sites
      </Link>
      <header className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-2xl font-bold">{s.domain}</h1>
          <p className="mt-1 text-sm text-muted">
            {plural(visible.length, "endpoint")} · {verified} verified · {plural(s.runs.length, "run")}
            {last ? ` · last ${timeAgo(last.finished_at ?? last.started_at)}` : ""}
          </p>
        </div>
        <Button onClick={() => startAgain.mutate()} disabled={startAgain.isPending || state.data?.running != null || !last}>
          <RefreshCw className="size-4" /> Run again
        </Button>
      </header>
      {startAgain.error && <ErrorBox error={startAgain.error} />}

      <Tabs
        value={tab}
        onChange={(id) => setParams(id === "endpoints" ? {} : { tab: id }, { replace: true })}
        tabs={[
          { id: "endpoints", label: "Endpoints", count: visible.length },
          { id: "code", label: "Client code" },
          { id: "docs", label: "API docs" },
          { id: "report", label: "Report" },
          { id: "runs", label: <span className="inline-flex items-center gap-1"><History className="size-3.5" />Runs</span>, count: s.runs.length },
          { id: "lessons", label: "Lessons", count: s.lessons.length },
        ]}
      />

      {tab === "endpoints" && <EndpointsTab site={s} />}
      {tab === "code" && <CodeTab site={s} languages={state.data?.languages ?? ["python", "typescript"]} />}
      {tab === "docs" && <DocsTab site={s} />}
      {tab === "report" && <ReportTab site={s} />}
      {tab === "runs" && <RunsTab site={s} />}
      {tab === "lessons" && <LessonsTab site={s} />}

      <p className="text-xs text-muted">
        Files: {s.files.map((f) => `${f.name} (${bytes(f.size)})`).join(" · ") || "none"}
      </p>
    </div>
  );
}
