import { Bot, Download, ExternalLink, Eye, Sheet } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import type { DataFormat, EndpointDetail } from "../types";
import { CopyButton } from "./CodeBlock";
import { inputsOf } from "./TryIt";
import { Button, Card, EmptyState, ErrorBox } from "./ui";

/** Where `ghost-api serve` listens unless given --port. */
const SERVE_URL = "http://127.0.0.1:8900";

function Command({ text }: { text: string }) {
  return (
    <div className="flex items-start gap-2 rounded-lg border border-line bg-surface-2/50 px-3 py-2">
      <code className="min-w-0 flex-1 font-mono text-xs leading-relaxed break-all">{text}</code>
      <CopyButton text={text} />
    </div>
  );
}

function Way({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return (
    <Card className="space-y-2 p-4">
      <h3 className="flex items-center gap-2 font-semibold">
        <span className="text-accent">{icon}</span>
        {title}
      </h3>
      {children}
    </Card>
  );
}

const quote = (s: string) => (/[\s"[\]{}]/.test(s) ? `"${s.replace(/"/g, '\\"')}"` : s);

/** Download the example operation's data in the browser, with its example inputs. */
function DownloadNow({ site, endpoint }: { site: string; endpoint: EndpointDetail }) {
  const [format, setFormat] = useState<DataFormat>("xlsx");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<{ rows: number; filename: string } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const download = async () => {
    setBusy(true);
    setDone(null);
    setError(null);
    const params = Object.fromEntries(
      inputsOf(endpoint)
        .filter((i) => i.example !== "")
        .map((i) => [i.name, i.example]),
    );
    try {
      setDone(await api.fetchData(site, endpoint.key, params, 5, format));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="primary" onClick={() => void download()} disabled={busy}>
          <Download className="size-4" />
          {busy ? "Downloading…" : "Download now"}
        </Button>
        <select
          value={format}
          onChange={(ev) => setFormat(ev.target.value as DataFormat)}
          aria-label="File format"
          className="rounded-lg border border-line bg-surface px-2 py-1.5 text-sm"
        >
          <option value="xlsx">Excel</option>
          <option value="csv">CSV</option>
          <option value="json">JSON</option>
        </select>
        <span className="text-xs text-muted">up to 5 pages, example inputs</span>
      </div>
      {error !== null && <ErrorBox error={error} />}
      {done && (
        <p className="text-sm text-ok" aria-live="polite">
          Downloaded {done.rows} rows as {done.filename}. For other inputs, use Endpoints, then Try it.
        </p>
      )}
    </div>
  );
}

/** Whether something answers at SERVE_URL; null until the first check. Rechecks every 5 s. */
function useServeRunning(): boolean | null {
  const [running, setRunning] = useState<boolean | null>(null);
  useEffect(() => {
    let cancelled = false;
    const check = () =>
      // no-cors: the reply is unreadable, but the request only fails when nothing listens.
      fetch(`${SERVE_URL}/`, { mode: "no-cors", cache: "no-store" }).then(
        () => !cancelled && setRunning(true),
        () => !cancelled && setRunning(false),
      );
    void check();
    const id = setInterval(check, 5000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);
  return running;
}

function ServeStatus({ url }: { url: string }) {
  const running = useServeRunning();
  if (running === null) return null;
  if (!running) {
    return (
      <p className="text-xs text-warn" aria-live="polite">
        Not running yet: run the command above first and keep its window open. The link works while it runs.
      </p>
    );
  }
  return (
    <p className="flex items-center gap-2 text-xs text-ok" aria-live="polite">
      Running.
      <a href={url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 underline">
        Open the link <ExternalLink className="size-3" />
      </a>
    </p>
  );
}

/** Ways to use a site's data elsewhere: files, spreadsheets, AI, alerts. */
export function OtherTools({ site, endpoints }: { site: string; endpoints: EndpointDetail[] }) {
  // Prefer an endpoint whose live test passed, even on another service (goboult's
  // products come from api.wizsearch.in), over an unverified one on the site itself.
  const usable = endpoints
    .filter((e) => e.callable && e.operation)
    .map((e, index) => ({ e, index, rank: (e.status === "verified" ? 0 : 2) + (e.third_party ? 1 : 0) }))
    .sort((a, b) => a.rank - b.rank || a.index - b.index)
    .map(({ e }) => e);
  const [chosen, setChosen] = useState<string | null>(null);
  const op = usable.find((e) => e.key === chosen) ?? usable[0];
  if (!op || !op.operation) {
    return (
      <EmptyState title="Nothing here can be called outside the browser">
        Endpoints Ghost API can call (read-only) show up here with ready-to-copy commands.
      </EmptyState>
    );
  }
  const inputs = (op.inputs ?? [])
    .filter((i) => i.example && i.location !== "query")
    .slice(0, 3)
    .map((i) => quote(`${i.name}=${i.example}`))
    .join(" ");
  const args = [quote(site), op.operation, inputs].filter(Boolean).join(" ");
  const restPath = `${site.replace("/", "~")}/${op.operation}`;
  const restUrl = `${SERVE_URL}/${restPath}?format=csv`;
  const slug = site.split("/")[0].replace(/^www\./, "").split(".")[0];
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <label htmlFor="other-tools-op" className="text-muted">
          Operation
        </label>
        {usable.length > 1 ? (
          <select
            id="other-tools-op"
            value={op.key}
            onChange={(ev) => setChosen(ev.target.value)}
            className="rounded-lg border border-line bg-surface px-2 py-1.5 font-mono text-sm"
          >
            {usable.map((e) => (
              <option key={e.key} value={e.key}>
                {e.operation} ({e.status}
                {e.third_party ? `, ${e.host}` : ""})
              </option>
            ))}
          </select>
        ) : (
          <span id="other-tools-op" className="font-mono">
            {op.operation}
          </span>
        )}
        <span className="text-xs text-muted">{op.label}</span>
      </div>
      <p className="text-sm text-muted">
        The commands run in Command Prompt or PowerShell (type them anywhere once{" "}
        <code className="font-mono">scripts\install-command.cmd</code> has run).
      </p>
      <div className="grid gap-4 lg:grid-cols-2">
        <Way icon={<Download className="size-4" />} title="Download the data (CSV, Excel, JSON)">
          <DownloadNow key={op.key} site={site} endpoint={op} />
          <p className="text-xs text-muted">Or from the command line, for scripts:</p>
          <Command text={`ghost-api fetch ${quote(site)}`} />
          <Command text={`ghost-api fetch ${args} --format xlsx`} />
        </Way>
        <Way icon={<Sheet className="size-4" />} title="As a web API (Excel, scripts, any language)">
          <p className="text-xs text-muted">1. Start the server (keep the window open):</p>
          <Command text={`ghost-api serve ${quote(site)}`} />
          <p className="text-xs text-muted">2. Then open (docs at /docs):</p>
          <Command text={restUrl} />
          <ServeStatus url={restUrl} />
        </Way>
        <Way icon={<Bot className="size-4" />} title="As tools for an AI assistant (MCP)">
          <Command text={`claude mcp add ghost-${slug} -- ghost-api mcp ${quote(site)}`} />
          <p className="text-xs text-muted">
            Then ask Claude things like "use ghost-{slug} to …". Needs the MCP extra:{" "}
            <code className="font-mono">pipx install --force "ghost-api-cli[mcp]"</code> (from a source
            checkout: <code className="font-mono">pip install -e ".[mcp]"</code>).
          </p>
        </Way>
        <Way icon={<Eye className="size-4" />} title="Get alerts when something changes">
          <Command text={`ghost-api watch add ${args} --every 6h --notify ntfy:your-long-private-topic`} />
          <p className="text-xs text-muted">Or use "Watch this for changes" on an endpoint.</p>
        </Way>
      </div>
    </div>
  );
}
