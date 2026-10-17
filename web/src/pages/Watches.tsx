import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BellRing, Eye, Minus, Plus, RefreshCw, Trash2 } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Button, Card, cx, EmptyState, ErrorBox, Pill, SectionTitle, Skeleton, timeAgo } from "../components/ui";
import type { Watch, WatchChange } from "../types";

const KIND: Record<WatchChange["kind"], { icon: typeof Plus; className: string; label: string }> = {
  new: { icon: Plus, className: "text-ok", label: "New" },
  changed: { icon: RefreshCw, className: "text-info", label: "Changed" },
  gone: { icon: Minus, className: "text-bad", label: "Gone" },
};

const show = (v: unknown) => (v === null || v === undefined || v === "" ? "n/a" : String(v));

export function ChangeLine({ change }: { change: WatchChange }) {
  const { icon: Icon, className, label } = KIND[change.kind];
  return (
    <li className="flex gap-3 py-2">
      <Icon className={cx("mt-0.5 size-4 shrink-0", className)} aria-label={label} />
      <div className="min-w-0 flex-1 text-sm">
        <span className="font-medium break-words">{change.title}</span>
        {change.kind === "changed" && (
          <ul className="mt-0.5 space-y-0.5 text-xs text-muted">
            {Object.entries(change.fields).map(([name, [before, after]]) => (
              <li key={name} className="break-words">
                <span className="font-mono">{name}</span>: {show(before)} →{" "}
                <span className="text-fg">{show(after)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
      {change.at && <span className="shrink-0 text-xs text-muted">{timeAgo(change.at)}</span>}
    </li>
  );
}

function WatchCard({ watch, changes }: { watch: Watch; changes: WatchChange[] }) {
  const qc = useQueryClient();
  const refresh = () => void qc.invalidateQueries({ queryKey: ["watches"] });
  const check = useMutation({ mutationFn: () => api.checkWatch(watch.id), onSuccess: refresh });
  const remove = useMutation({ mutationFn: () => api.removeWatch(watch.id), onSuccess: refresh });
  const failed = watch.last_status?.startsWith("error");
  const inputs = Object.entries(watch.params);
  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-muted">#{watch.id}</span>
            <Link to={`/sites/${watch.site_id}`} className="font-semibold hover:text-accent">
              {watch.site}
            </Link>
            <span className="font-mono text-sm text-accent">{watch.operation}</span>
          </div>
          {inputs.length > 0 && (
            <p className="mt-1 font-mono text-xs break-all text-muted">
              {inputs.map(([k, v]) => `${k}=${v}`).join("  ")}
            </p>
          )}
          <div className="mt-2 flex flex-wrap gap-1.5">
            <Pill>every {watch.every}</Pill>
            {watch.notify.map((n) => (
              <Pill key={n} className="bg-accent-soft text-accent">
                <BellRing className="size-3" /> {n.split(":")[0]}
              </Pill>
            ))}
            <Pill className={failed ? "bg-bad/15 text-bad" : undefined}>
              {watch.last_check_at ? `checked ${timeAgo(watch.last_check_at)}` : "not checked yet"}
              {watch.last_status ? ` · ${watch.last_status}` : ""}
            </Pill>
          </div>
        </div>
        <div className="flex gap-1.5">
          <Button onClick={() => check.mutate()} disabled={check.isPending}>
            <RefreshCw className={cx("size-4", check.isPending && "animate-spin")} />
            {check.isPending ? "Checking…" : "Check now"}
          </Button>
          <Button variant="ghost" onClick={() => remove.mutate()} aria-label={`Remove watch ${watch.id}`}>
            <Trash2 className="size-4" />
          </Button>
        </div>
      </div>
      {check.data && <p className="mt-2 text-sm text-muted" aria-live="polite">{check.data.headline}</p>}
      {(check.error || remove.error) && <ErrorBox error={check.error ?? remove.error} />}
      {changes.length > 0 && (
        <ul className="mt-3 divide-y divide-line border-t border-line">
          {changes.slice(0, 6).map((c, i) => (
            <ChangeLine key={`${c.key}-${c.at}-${i}`} change={c} />
          ))}
        </ul>
      )}
    </Card>
  );
}

export function Watches() {
  const query = useQuery({ queryKey: ["watches"], queryFn: api.watches, refetchInterval: 30_000 });
  return (
    <div className="mx-auto max-w-4xl space-y-5">
      <div>
        <h1 className="text-2xl font-bold">Watches</h1>
        <p className="mt-1 text-sm text-muted">
          Ghost API re-checks these on a schedule and tells you what's new, what changed (a price, a
          name...) and what disappeared. Checks run while this dashboard is open, or in the
          background after <code className="font-mono">ghost-api watch schedule</code>.
        </p>
      </div>
      {query.isPending && <Skeleton className="h-40" />}
      {query.error && <ErrorBox error={query.error} />}
      {query.data && query.data.watches.length === 0 && (
        <Card>
          <EmptyState icon={<Eye className="size-8" />} title="No watches yet">
            Open a site, pick an endpoint, and press "Watch this" under "Use this endpoint".
          </EmptyState>
        </Card>
      )}
      {query.data?.watches.map((w) => (
        <WatchCard
          key={w.id}
          watch={w}
          changes={query.data.changes.filter((c) => c.watch_id === w.id)}
        />
      ))}
      {query.data && query.data.changes.length > 0 && (
        <Card>
          <SectionTitle>Recent changes, all watches</SectionTitle>
          <ul className="divide-y divide-line px-4">
            {query.data.changes.slice(0, 30).map((c, i) => (
              <ChangeLine key={`${c.watch_id}-${c.key}-${c.at}-${i}`} change={c} />
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
