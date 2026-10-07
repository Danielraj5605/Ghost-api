import { Bot, Download, Eye, Sheet } from "lucide-react";
import type { ReactNode } from "react";
import type { EndpointDetail } from "../types";
import { CopyButton } from "./CodeBlock";
import { Card, EmptyState } from "./ui";

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

/** Copy-paste commands to use a site's data elsewhere: files, spreadsheets, AI, alerts. */
export function OtherTools({ site, endpoints }: { site: string; endpoints: EndpointDetail[] }) {
  const usable = endpoints.filter((e) => e.callable && e.operation && !e.third_party);
  const op = usable[0] ?? endpoints.find((e) => e.callable && e.operation);
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
  const slug = site.split("/")[0].replace(/^www\./, "").split(".")[0];
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted">
        Run these in Command Prompt or PowerShell (type them anywhere once{" "}
        <code className="font-mono">scripts\install-command.cmd</code> has run). Examples use{" "}
        <span className="font-mono">{op.operation}</span>; {usable.length > 1 ? `${usable.length} operations are available.` : "it's the one available."}
      </p>
      <div className="grid gap-4 lg:grid-cols-2">
        <Way icon={<Download className="size-4" />} title="Download the data (CSV, Excel, JSON)">
          <Command text={`ghost-api fetch ${quote(site)}`} />
          <Command text={`ghost-api fetch ${args} --format xlsx`} />
        </Way>
        <Way icon={<Sheet className="size-4" />} title="As a web API (Excel, scripts, any language)">
          <Command text={`ghost-api serve ${quote(site)}`} />
          <p className="text-xs text-muted">Then open (docs at /docs):</p>
          <Command text={`http://127.0.0.1:8900/${restPath}?format=csv`} />
        </Way>
        <Way icon={<Bot className="size-4" />} title="As tools for an AI assistant (MCP)">
          <Command text={`claude mcp add ghost-${slug} -- ghost-api mcp ${quote(site)}`} />
          <p className="text-xs text-muted">
            Then ask Claude things like "use ghost-{slug} to …". Needs <code className="font-mono">pip install mcp</code>.
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
