import { MonitorPlay } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import type { Box } from "../types";
import { EmptyState } from "./ui";

/** The agent's latest screenshot, with the element it clicked outlined. */
export function LiveBrowser({
  runId,
  shot,
  url,
  live,
}: {
  runId: number | null;
  shot: { name: string; box: Box | null } | null;
  url: string;
  live: boolean;
}) {
  const [failed, setFailed] = useState<string | null>(null);
  const box = shot?.box ?? null;

  return (
    <div className="overflow-hidden rounded-xl border border-line bg-surface">
      <div className="flex items-center gap-2 border-b border-line bg-surface-2/60 px-3 py-2">
        <span className="flex gap-1" aria-hidden>
          <span className="size-2.5 rounded-full bg-bad/60" />
          <span className="size-2.5 rounded-full bg-warn/60" />
          <span className="size-2.5 rounded-full bg-ok/60" />
        </span>
        <span className="min-w-0 flex-1 truncate rounded-md bg-surface px-2 py-0.5 font-mono text-xs text-muted">
          {url || "about:blank"}
        </span>
        {live && (
          <span className="flex items-center gap-1 text-xs font-medium text-bad">
            <span className="size-1.5 animate-pulse rounded-full bg-bad" />
            LIVE
          </span>
        )}
      </div>
      <div className="relative aspect-video bg-surface-2">
        {runId !== null && shot && failed !== shot.name ? (
          <>
            <img
              key={shot.name}
              src={api.screenUrl(runId, shot.name)}
              alt={`What the agent saw (${shot.name})`}
              onError={() => setFailed(shot.name)}
              className="absolute inset-0 size-full object-contain"
            />
            {box && box.viewport_width > 0 && (
              <div
                className="pointer-events-none absolute rounded-md border-2 border-accent shadow-[0_0_0_4px_color-mix(in_oklab,var(--accent)_25%,transparent)]"
                style={{
                  left: `${(box.x / box.viewport_width) * 100}%`,
                  top: `${(box.y / box.viewport_height) * 100}%`,
                  width: `${(box.width / box.viewport_width) * 100}%`,
                  height: `${(box.height / box.viewport_height) * 100}%`,
                }}
                aria-hidden
              />
            )}
          </>
        ) : (
          <div className="absolute inset-0 flex items-center justify-center">
            <EmptyState icon={<MonitorPlay className="size-8" />} title="No screenshot yet">
              The agent's view of the page appears here as it works.
            </EmptyState>
          </div>
        )}
      </div>
    </div>
  );
}
