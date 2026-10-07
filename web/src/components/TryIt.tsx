import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Download, Play, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import type { DataFormat, EndpointDetail, EndpointInput, TryResult } from "../types";
import { CodeBlock } from "./CodeBlock";
import { Button, ErrorBox, Pill } from "./ui";

/** Inputs from the server, or (older data) from the endpoint's path/query parameters. */
function inputsOf(endpoint: EndpointDetail): EndpointInput[] {
  if (endpoint.inputs) return endpoint.inputs;
  return endpoint.params
    .filter((p) => !p.sensitive)
    .map((p) => ({
      name: p.name,
      location: p.location,
      type: p.type,
      example: p.examples[0] ?? "",
      required: p.location === "path",
    }));
}

/** "I checked, it only reads": for a POST search Ghost API won't call by itself. */
function AllowSwitch({ domain, endpoint }: { domain: string; endpoint: EndpointDetail }) {
  const qc = useQueryClient();
  const allow = useMutation({
    mutationFn: () => api.allow(domain, endpoint.key, true),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["site", domain] }),
  });
  const canAllow = endpoint.method === "POST" && endpoint.replayable !== false;
  return (
    <div className="space-y-2 rounded-lg bg-surface-2 px-3 py-2.5 text-sm">
      <p className="text-muted">
        {canAllow
          ? "Ghost API doesn't call this endpoint by itself: it's a POST, the kind sites use to change things (book, pay, post). If you checked that it only searches or reads, you can allow it."
          : "Ghost API doesn't call this endpoint: it looks like it changes data, or it only works inside the browser."}
      </p>
      {canAllow && (
        <Button onClick={() => allow.mutate()} disabled={allow.isPending}>
          <ShieldCheck className="size-4" />
          {allow.isPending ? "Allowing…" : "I checked: it only reads, allow it"}
        </Button>
      )}
      {allow.error && <ErrorBox error={allow.error} />}
    </div>
  );
}

/** Call an endpoint once ("Send request"), or download all its pages ("Download data"). */
export function TryIt({ domain, endpoint }: { domain: string; endpoint: EndpointDetail }) {
  const inputs = inputsOf(endpoint);
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(inputs.map((i) => [i.name, i.example])),
  );
  const [pages, setPages] = useState(5);
  const [format, setFormat] = useState<DataFormat>("csv");
  const params = () => Object.fromEntries(Object.entries(values).filter(([, v]) => v !== ""));

  const run = useMutation<TryResult, Error>({
    mutationFn: () => api.tryEndpoint(domain, endpoint.key, params()),
  });
  const download = useMutation({
    mutationFn: () => api.fetchData(domain, endpoint.key, params(), pages, format),
  });

  if (endpoint.callable === false || (endpoint.callable === undefined && endpoint.replayable === false)) {
    return <AllowSwitch domain={domain} endpoint={endpoint} />;
  }

  const pretty = (() => {
    if (!run.data) return "";
    try {
      return JSON.stringify(JSON.parse(run.data.body), null, 2);
    } catch {
      return run.data.body;
    }
  })();
  const field =
    "w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 font-mono text-sm outline-none focus:border-accent";

  return (
    <div className="space-y-3">
      {inputs.length > 0 && (
        <div className="grid gap-2 sm:grid-cols-2">
          {inputs.map((i) => (
            <label key={i.name} className="text-sm">
              <span className="mb-1 flex items-center gap-1.5">
                <span className="font-mono">{i.name}</span>
                <span className="text-xs text-muted">
                  {i.location} · {i.type}
                </span>
              </span>
              <input
                value={values[i.name] ?? ""}
                onChange={(ev) => setValues({ ...values, [i.name]: ev.target.value })}
                placeholder={i.example}
                className={field}
              />
            </label>
          ))}
        </div>
      )}
      {inputs.some((i) => i.type === "list") && (
        <p className="text-xs text-muted">For list inputs, write one value or several separated by commas.</p>
      )}

      <div className="flex flex-wrap items-end gap-2">
        <Button variant="primary" onClick={() => run.mutate()} disabled={run.isPending}>
          <Play className="size-4" />
          {run.isPending ? "Calling…" : "Send request"}
        </Button>
        <span className="mx-1 hidden h-6 w-px bg-line sm:block" aria-hidden />
        <label className="text-xs text-muted">
          Pages
          <input
            type="number"
            min={1}
            max={50}
            value={pages}
            onChange={(ev) => setPages(Math.max(1, Math.min(50, Number(ev.target.value) || 1)))}
            className="ml-1.5 w-16 rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-fg"
          />
        </label>
        <select
          value={format}
          onChange={(ev) => setFormat(ev.target.value as DataFormat)}
          aria-label="File format"
          className="rounded-lg border border-line bg-surface px-2 py-1.5 text-sm"
        >
          <option value="csv">CSV</option>
          <option value="xlsx">Excel</option>
          <option value="json">JSON</option>
        </select>
        <Button onClick={() => download.mutate()} disabled={download.isPending}>
          <Download className="size-4" />
          {download.isPending ? "Downloading…" : "Download data"}
        </Button>
      </div>
      {download.error && <ErrorBox error={download.error} />}
      {download.data && (
        <p className="text-sm text-ok" aria-live="polite">
          Downloaded {download.data.rows} rows as {download.data.filename}.
        </p>
      )}

      {run.error && <ErrorBox error={run.error} />}
      {run.data && (
        <div className="space-y-2" aria-live="polite">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Pill className={run.data.ok ? "bg-ok/15 text-ok" : "bg-bad/15 text-bad"}>
              {run.data.status || "no response"}
            </Pill>
            <span className="text-muted">{run.data.ms} ms</span>
            <span className="min-w-0 truncate font-mono text-xs text-muted">{run.data.url}</span>
          </div>
          {run.data.error && <ErrorBox error={run.data.error} />}
          {pretty && (
            <CodeBlock
              code={pretty}
              language="json"
              title={run.data.truncated ? "response (cut short)" : "response"}
              maxHeight="24rem"
            />
          )}
        </div>
      )}
    </div>
  );
}
