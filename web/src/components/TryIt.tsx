import { useMutation } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import type { EndpointDetail, TryResult } from "../types";
import { CodeBlock } from "./CodeBlock";
import { Button, ErrorBox, Pill } from "./ui";

export function TryIt({ domain, endpoint }: { domain: string; endpoint: EndpointDetail }) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(endpoint.params.map((p) => [p.name, p.sensitive ? "" : (p.examples[0] ?? "")])),
  );
  const run = useMutation<TryResult, Error>({
    mutationFn: () => {
      const params = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== ""));
      return api.tryEndpoint(domain, endpoint.key, params);
    },
  });

  if (endpoint.replayable === false) {
    return (
      <p className="rounded-lg bg-surface-2 px-3 py-2 text-sm text-muted">
        Try it is off for this endpoint: it looks like it changes data on the site, so Ghost API
        never calls it on its own.
      </p>
    );
  }

  const pretty = (() => {
    if (!run.data) return "";
    try {
      return JSON.stringify(JSON.parse(run.data.body), null, 2);
    } catch {
      return run.data.body;
    }
  })();

  return (
    <div className="space-y-3">
      {endpoint.params.length > 0 && (
        <div className="grid gap-2 sm:grid-cols-2">
          {endpoint.params.map((p) => (
            <label key={p.name} className="text-sm">
              <span className="mb-1 flex items-center gap-1.5">
                <span className="font-mono">{p.name}</span>
                <span className="text-xs text-muted">
                  {p.location} · {p.type}
                </span>
              </span>
              <input
                value={values[p.name] ?? ""}
                onChange={(ev) => setValues({ ...values, [p.name]: ev.target.value })}
                placeholder={p.sensitive ? "hidden (sensitive)" : (p.examples[0] ?? "")}
                className="w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 font-mono text-sm outline-none focus:border-accent"
              />
            </label>
          ))}
        </div>
      )}
      <Button variant="primary" onClick={() => run.mutate()} disabled={run.isPending}>
        <Play className="size-4" />
        {run.isPending ? "Calling…" : "Send request"}
      </Button>
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
