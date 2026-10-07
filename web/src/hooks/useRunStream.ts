import { useEffect, useReducer, useState } from "react";
import { api } from "../api";
import { initialRunState, runReducer } from "../state/runReducer";
import type { GhostEvent } from "../types";

export type StreamStatus = "connecting" | "live" | "ended" | "error";

/** Follows a run's events: stored ones first, then live ones, until the run ends. */
export function useRunStream(runId: number | null) {
  const [state, dispatch] = useReducer(runReducer, initialRunState);
  const [status, setStatus] = useState<StreamStatus>("connecting");

  useEffect(() => {
    dispatch({ type: "reset" });
    if (runId === null) return;
    setStatus("connecting");
    const source = new EventSource(api.eventsUrl(runId));
    source.onopen = () => setStatus("live");
    source.onmessage = (message: MessageEvent<string>) => {
      try {
        dispatch({ type: "event", event: JSON.parse(message.data) as GhostEvent });
      } catch {
        /* ignore a malformed line */
      }
    };
    source.addEventListener("end", () => {
      setStatus("ended");
      source.close();
    });
    // EventSource reconnects by itself; the reducer skips events it already has.
    source.onerror = () => setStatus((s) => (s === "ended" ? s : "error"));
    return () => source.close();
  }, [runId]);

  return { state, status };
}
