import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowRight, Bot, ChevronDown, Globe, Sparkles, Target } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { GhostMark } from "../components/Logo";
import { Button, Card, cx, EmptyState, ErrorBox, MethodBadge, Pill, plural, Skeleton, timeAgo } from "../components/ui";
import type { SiteSummary, StartRunRequest } from "../types";

const EXAMPLES = [
  { name: "Quotes", url: "https://quotes.toscrape.com/scroll", goal: "list quotes with their authors and tags" },
  { name: "Hacker News search", url: "https://hn.algolia.com/", goal: "search Hacker News stories and comments" },
  { name: "Books", url: "https://books.toscrape.com/", goal: "list books and get book details" },
];

const STATUS_TONE: Record<string, string> = {
  failed: "bg-bad/15 text-bad",
  interrupted: "bg-warn/15 text-warn",
  running: "bg-accent-soft text-accent",
};

function NewRunForm({ hasLlm, running }: { hasLlm: boolean; running: number | null }) {
  const navigate = useNavigate();
  const [url, setUrl] = useState("");
  const [goal, setGoal] = useState("");
  const [lang, setLang] = useState("python");
  const [agent, setAgent] = useState(true);
  const [maxSteps, setMaxSteps] = useState(20);
  const [maxCalls, setMaxCalls] = useState(30);
  const [advanced, setAdvanced] = useState(false);

  const start = useMutation({
    mutationFn: (body: StartRunRequest) => api.startRun(body),
    onSuccess: ({ run_id }) => navigate(`/runs/${run_id}`),
  });

  const submit = (ev: FormEvent) => {
    ev.preventDefault();
    const full = /^https?:\/\//i.test(url.trim()) ? url.trim() : `https://${url.trim()}`;
    start.mutate({
      url: full,
      goal: goal.trim(),
      lang,
      agent: agent && hasLlm,
      max_steps: maxSteps,
      max_llm_calls: maxCalls,
      ignore_robots: false,
    });
  };

  const field =
    "flex items-center gap-2.5 rounded-xl border border-line bg-surface px-3 py-2.5 transition focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/20";

  return (
    <form onSubmit={submit} className="mx-auto w-full max-w-2xl space-y-3 text-left">
      <label className={field}>
        <Globe className="size-5 shrink-0 text-muted" aria-hidden />
        <span className="sr-only">Website URL</span>
        <input
          required
          value={url}
          onChange={(ev) => setUrl(ev.target.value)}
          placeholder="https://quotes.toscrape.com/scroll"
          inputMode="url"
          autoComplete="url"
          className="w-full bg-transparent text-base outline-none placeholder:text-muted"
        />
      </label>
      <label className={field}>
        <Target className="size-5 shrink-0 text-muted" aria-hidden />
        <span className="sr-only">Goal</span>
        <input
          required
          value={goal}
          onChange={(ev) => setGoal(ev.target.value)}
          placeholder="What data do you want? e.g. list quotes with their authors"
          className="w-full bg-transparent text-base outline-none placeholder:text-muted"
        />
      </label>

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted">Try:</span>
        {EXAMPLES.map((ex) => (
          <button
            key={ex.name}
            type="button"
            onClick={() => {
              setUrl(ex.url);
              setGoal(ex.goal);
            }}
            className="rounded-full border border-line bg-surface px-2.5 py-1 text-xs transition hover:border-accent hover:text-accent"
          >
            {ex.name}
          </button>
        ))}
      </div>

      <div className="rounded-xl border border-line bg-surface">
        <button
          type="button"
          onClick={() => setAdvanced(!advanced)}
          aria-expanded={advanced}
          className="flex w-full items-center justify-between px-3 py-2 text-sm text-muted hover:text-fg"
        >
          Options
          <ChevronDown className={cx("size-4 transition", advanced && "rotate-180")} />
        </button>
        {advanced && (
          <div className="grid gap-3 border-t border-line p-3 sm:grid-cols-2">
            <label className="text-sm">
              <span className="mb-1 block text-xs text-muted">Client language</span>
              <select
                value={lang}
                onChange={(ev) => setLang(ev.target.value)}
                className="w-full rounded-lg border border-line bg-surface px-2 py-1.5"
              >
                <option value="python">Python</option>
                <option value="typescript">TypeScript</option>
              </select>
            </label>
            <label className="flex items-center gap-2 self-end text-sm">
              <input type="checkbox" checked={agent && hasLlm} disabled={!hasLlm} onChange={(ev) => setAgent(ev.target.checked)} />
              <span>
                Let the AI agent explore
                {!hasLlm && <span className="block text-xs text-warn">Needs an LLM key in .env</span>}
              </span>
            </label>
            <label className="text-sm">
              <span className="mb-1 block text-xs text-muted">Max agent steps: {maxSteps}</span>
              <input type="range" min={1} max={60} value={maxSteps} onChange={(ev) => setMaxSteps(+ev.target.value)} className="w-full accent-[var(--accent)]" />
            </label>
            <label className="text-sm">
              <span className="mb-1 block text-xs text-muted">Max LLM calls: {maxCalls}</span>
              <input type="range" min={1} max={100} value={maxCalls} onChange={(ev) => setMaxCalls(+ev.target.value)} className="w-full accent-[var(--accent)]" />
            </label>
          </div>
        )}
      </div>

      {start.error && <ErrorBox error={start.error} />}
      {running !== null ? (
        <p className="text-center text-sm text-muted">
          A run is already in progress.{" "}
          <Link to={`/runs/${running}`} className="font-medium text-accent hover:underline">
            Watch run #{running}
          </Link>
        </p>
      ) : (
        <Button type="submit" variant="primary" disabled={start.isPending} className="w-full py-2.5 text-base">
          <Sparkles className="size-4" />
          {start.isPending ? "Starting…" : "Find the API"}
          <ArrowRight className="size-4" />
        </Button>
      )}
      <p className="text-center text-xs text-muted">
        Ghost API respects robots.txt and stops at logins or bot checks. Only scan sites you are allowed to.
      </p>
    </form>
  );
}

function SiteCard({ site }: { site: SiteSummary }) {
  const tone = STATUS_TONE[site.latest.status] ?? "bg-ok/15 text-ok";
  return (
    <Link
      to={`/sites/${encodeURIComponent(site.domain)}`}
      className="group block rounded-xl border border-line bg-surface p-4 transition hover:-translate-y-0.5 hover:border-accent/50 hover:shadow-lg hover:shadow-accent/5"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate font-semibold group-hover:text-accent">{site.domain}</h3>
          <p className="mt-0.5 truncate text-xs text-muted" title={site.latest.goal}>
            {site.latest.goal}
          </p>
        </div>
        <Pill className={tone}>{site.latest.status}</Pill>
      </div>
      <div className="mt-3 flex flex-wrap gap-1">
        {site.methods.map((m) => (
          <MethodBadge key={m} method={m} />
        ))}
      </div>
      <div className="mt-3 flex items-center gap-3 text-xs text-muted">
        <span className="font-medium text-fg">{plural(site.endpoints, "endpoint")}</span>
        {site.counts.verified > 0 && <span className="text-ok">{site.counts.verified} verified</span>}
        <span>{plural(site.runs, "run")}</span>
        <span className="ml-auto">{timeAgo(site.latest.finished_at ?? site.latest.started_at)}</span>
      </div>
    </Link>
  );
}

export function Home() {
  const state = useQuery({ queryKey: ["state"], queryFn: api.state });
  const sites = useQuery({ queryKey: ["sites"], queryFn: api.sites });

  return (
    <div className="space-y-12">
      <section className="ghost-glow -mx-4 -mt-6 px-4 pt-12 pb-4 text-center sm:pt-16">
        <GhostMark className="mx-auto size-14" />
        <h1 className="mt-4 text-3xl font-bold tracking-tight sm:text-4xl">
          Turn any website into an <span className="text-accent">API client</span>
        </h1>
        <p className="mx-auto mt-3 max-w-xl text-muted">
          An AI agent browses the site, watches the hidden API calls it makes, tests them, and writes a typed,
          tested client you can use from code.
        </p>
        <div className="mt-8">
          <NewRunForm hasLlm={state.data?.has_llm ?? true} running={state.data?.running ?? null} />
        </div>
      </section>

      <section>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg font-semibold">Your sites</h2>
          {sites.data && sites.data.length > 0 && <span className="text-sm text-muted">{plural(sites.data.length, "site")}</span>}
        </div>
        {sites.isPending && (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-36" />
            ))}
          </div>
        )}
        {sites.error && <ErrorBox error={sites.error} />}
        {sites.data?.length === 0 && (
          <Card>
            <EmptyState icon={<Bot className="size-8" />} title="No sites yet">
              Start a run above. When it finishes, the site's endpoints, client code and report show up here.
            </EmptyState>
          </Card>
        )}
        {sites.data && sites.data.length > 0 && (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {sites.data.map((s) => (
              <SiteCard key={s.domain} site={s} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
