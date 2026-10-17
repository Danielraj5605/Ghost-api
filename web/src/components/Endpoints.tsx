import { ArrowRightLeft, Layers, Lock, Search, X } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import type { EndpointDetail, EndpointSummary } from "../types";
import { ExposureBadge, ExposurePanel } from "./Exposure";
import { SchemaTree } from "./SchemaTree";
import {
  Button,
  ConfidenceBadge,
  cx,
  EmptyState,
  MethodBadge,
  Pill,
  plural,
  StatusBadge,
} from "./ui";

export function EndpointCard({
  endpoint,
  selected,
  onSelect,
  compact,
}: {
  endpoint: EndpointSummary;
  selected?: boolean;
  onSelect?: () => void;
  compact?: boolean;
}) {
  const e = endpoint;
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cx(
        "w-full rounded-xl border bg-surface p-3 text-left transition",
        selected ? "border-accent ring-2 ring-accent/20" : "border-line hover:border-accent/50",
      )}
    >
      <div className="flex items-center gap-2">
        <MethodBadge method={e.method} />
        <span className="min-w-0 flex-1 truncate font-mono text-sm" title={e.path}>
          {e.path}
        </span>
      </div>
      {!compact && (
        <>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <StatusBadge status={e.status} title={e.status_note} />
            <ConfidenceBadge confidence={e.confidence} />
            {e.pagination && (
              <Pill>
                <Layers className="size-3" /> {e.pagination.style}
              </Pill>
            )}
            {e.auth_type && (
              <Pill>
                <Lock className="size-3" /> {e.auth_type}
              </Pill>
            )}
            {e.third_party && <Pill>{e.host}</Pill>}
            {e.exposure && <ExposureBadge exposure={e.exposure} />}
          </div>
          {e.fields && <p className="mt-2 line-clamp-2 text-xs text-muted">{e.fields}</p>}
        </>
      )}
      {compact && (
        <div className="mt-1.5 flex items-center gap-1.5">
          <StatusBadge status={e.status} />
          <span className="text-xs text-muted">{plural(e.samples, "sample")}</span>
        </div>
      )}
    </button>
  );
}

/** A filterable list of endpoint cards. */
export function EndpointList({
  endpoints,
  selected,
  onSelect,
  compact,
}: {
  endpoints: EndpointSummary[];
  selected?: string | null;
  onSelect?: (key: string) => void;
  compact?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [showThirdParty, setShowThirdParty] = useState(false);
  const thirdParty = endpoints.filter((e) => e.third_party).length;
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return endpoints.filter(
      (e) =>
        (showThirdParty || !e.third_party) &&
        (!q || e.label.toLowerCase().includes(q) || e.fields.toLowerCase().includes(q)),
    );
  }, [endpoints, query, showThirdParty]);

  if (!endpoints.length) {
    return (
      <EmptyState icon={<ArrowRightLeft className="size-7" />} title="No endpoints yet">
        API calls the site makes appear here as they are captured.
      </EmptyState>
    );
  }
  return (
    <div className="space-y-2">
      {endpoints.length > 4 && (
        <label className="flex items-center gap-2 rounded-lg border border-line bg-surface px-2.5 py-1.5">
          <Search className="size-4 text-muted" aria-hidden />
          <input
            value={query}
            onChange={(ev) => setQuery(ev.target.value)}
            placeholder="Filter endpoints"
            aria-label="Filter endpoints"
            className="w-full bg-transparent text-sm outline-none placeholder:text-muted"
          />
        </label>
      )}
      {thirdParty > 0 && (
        <label className="flex items-center gap-2 text-xs text-muted">
          <input type="checkbox" checked={showThirdParty} onChange={(ev) => setShowThirdParty(ev.target.checked)} />
          Show {plural(thirdParty, "third-party endpoint")}
        </label>
      )}
      <ul className="space-y-2">
        {shown.map((e) => (
          <li key={e.key}>
            <EndpointCard
              endpoint={e}
              compact={compact}
              selected={selected === e.key}
              onSelect={onSelect ? () => onSelect(e.key) : undefined}
            />
          </li>
        ))}
      </ul>
      {!shown.length && <p className="py-4 text-center text-sm text-muted">No endpoint matches.</p>}
    </div>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 text-sm break-words">{children}</dd>
    </div>
  );
}

export function EndpointDetailPanel({
  endpoint,
  onClose,
  tryIt,
}: {
  endpoint: EndpointDetail;
  onClose?: () => void;
  /** The "Try it" form (dashboard only; the shared report can't call endpoints). */
  tryIt?: ReactNode;
}) {
  const e = endpoint;
  return (
    <article className="space-y-5" aria-label={e.label}>
      <header className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <MethodBadge method={e.method} />
            <h3 className="min-w-0 font-mono text-base font-semibold break-all">{e.path}</h3>
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            <StatusBadge status={e.status} />
            <ConfidenceBadge confidence={e.confidence} />
          </div>
          {e.status_note && <p className="mt-2 text-sm text-muted">{e.status_note}</p>}
        </div>
        {onClose && (
          <Button variant="ghost" onClick={onClose} aria-label="Close details" className="px-2">
            <X className="size-4" />
          </Button>
        )}
      </header>

      <dl className="grid grid-cols-2 gap-3 rounded-xl border border-line p-3 sm:grid-cols-4">
        <Fact label="Host">{e.host}</Fact>
        <Fact label="Seen">{plural(e.samples, "time")}</Fact>
        <Fact label="Statuses">{e.statuses.join(", ") || "none"}</Fact>
        <Fact label="Auth">{e.auth_type ?? "none"}</Fact>
        {e.pagination && (
          <Fact label="Pagination">
            {e.pagination.style} by <span className="font-mono">{e.pagination.param}</span>
            {e.pagination.confirmed ? " (tested)" : ""}
          </Fact>
        )}
        {e.required_headers.length > 0 && (
          <Fact label="Required headers">
            <span className="font-mono">{e.required_headers.join(", ")}</span>
          </Fact>
        )}
      </dl>

      {e.exposure && <ExposurePanel exposure={e.exposure} />}

      {e.params.length > 0 && (
        <section>
          <h4 className="mb-2 text-sm font-semibold">Parameters</h4>
          <div className="overflow-x-auto rounded-xl border border-line">
            <table className="w-full text-sm">
              <thead className="bg-surface-2/60 text-left text-xs text-muted">
                <tr>
                  <th className="px-3 py-2 font-medium">Name</th>
                  <th className="px-3 py-2 font-medium">In</th>
                  <th className="px-3 py-2 font-medium">Type</th>
                  <th className="px-3 py-2 font-medium">Examples</th>
                </tr>
              </thead>
              <tbody>
                {e.params.map((p) => (
                  <tr key={p.name} className="border-t border-line">
                    <td className="px-3 py-2 font-mono">{p.name}</td>
                    <td className="px-3 py-2 text-muted">{p.location}</td>
                    <td className="px-3 py-2 font-mono text-accent">{p.type}</td>
                    <td className="px-3 py-2 font-mono text-xs text-muted">
                      {p.sensitive ? "hidden" : p.examples.slice(0, 3).join(", ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {e.request_schema && (
        <section>
          <h4 className="mb-2 text-sm font-semibold">Request body</h4>
          <SchemaTree schema={e.request_schema} rootName="body" />
        </section>
      )}
      {e.response_schema && (
        <section>
          <h4 className="mb-2 text-sm font-semibold">Response</h4>
          <SchemaTree schema={e.response_schema} />
        </section>
      )}

      {e.example_urls.length > 0 && (
        <section>
          <h4 className="mb-2 text-sm font-semibold">Seen at</h4>
          <ul className="space-y-1 font-mono text-xs text-muted">
            {e.example_urls.slice(0, 5).map((u) => (
              <li key={u} className="break-all">
                {u}
              </li>
            ))}
          </ul>
        </section>
      )}

      {tryIt && (
        <section>
          <h4 className="mb-2 text-sm font-semibold">Use this endpoint</h4>
          {tryIt}
        </section>
      )}
    </article>
  );
}
