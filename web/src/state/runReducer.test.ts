import { describe, expect, it } from "vitest";
import type { GhostEvent } from "../types";
import { initialRunState, runReducer, stepStates, type RunState } from "./runReducer";

const play = (events: GhostEvent[], from: RunState = initialRunState) =>
  events.reduce((state, event) => runReducer(state, { type: "event", event }), from);

const started: GhostEvent = {
  type: "run_started",
  seq: 1,
  run_id: 7,
  domain: "shop.test",
  start_url: "https://shop.test/",
  goal: "list products",
  mode: "run",
  lang: "python",
};

describe("runReducer", () => {
  it("builds the run view from events", () => {
    const state = play([
      started,
      { type: "phase", seq: 2, phase: "explore" },
      { type: "request", seq: 3, method: "GET", url: "https://shop.test/api/p", status: 200, third_party: false },
      { type: "request", seq: 4, method: "GET", url: "https://cdn.test/x", status: 200, third_party: true },
      {
        type: "step",
        seq: 5,
        step: 1,
        action: "click",
        args: { element_id: 3 },
        thought: "Open the list",
        result: "ok",
        new_endpoints: ["GET /api/p"],
        requests: 2,
        screenshot: "step-1.jpg",
        box: { x: 1, y: 2, width: 3, height: 4, viewport_width: 1280, viewport_height: 720 },
      },
      { type: "notice", seq: 6, text: "Hello", level: "info", detail: "" },
    ]);
    expect(state.runId).toBe(7);
    expect(state.domain).toBe("shop.test");
    expect(state.requests).toBe(2);
    expect(state.thirdParty).toBe(1);
    expect(state.screenshot?.name).toBe("step-1.jpg");
    expect(state.screenshot?.box?.width).toBe(3);
    expect(state.timeline.map((t) => t.kind)).toEqual(["step", "notice"]);
    expect(state.phasesSeen).toEqual(["explore"]);
  });

  it("ignores events it already applied (after a reconnect)", () => {
    const once = play([started, { type: "notice", seq: 2, text: "a", level: "info", detail: "" }]);
    const twice = play([started, { type: "notice", seq: 2, text: "a", level: "info", detail: "" }], once);
    expect(twice.timeline).toHaveLength(1);
    expect(twice).toBe(once);
  });

  it("marks phases done, active, skipped and to-do", () => {
    const running = play([
      started,
      { type: "phase", seq: 2, phase: "explore" },
      { type: "phase", seq: 3, phase: "generate" },
    ]);
    expect(stepStates(running)).toEqual(["done", "skipped", "active", "todo", "todo"]);

    const finished = play(
      [
        { type: "phase", seq: 4, phase: "verify" },
        {
          type: "run_finished",
          seq: 5,
          status: "goal-covered",
          reason: "",
          seconds: 12,
          requests: 3,
          endpoints: 2,
          report: "r.md",
          openapi: "o.yaml",
          lessons_learned: 1,
          filtered: {},
          generation_error: "",
        },
      ],
      running,
    );
    expect(finished.phase).toBe("done");
    expect(stepStates(finished)).toEqual(["done", "skipped", "done", "done", "skipped"]);
  });

  it("keeps verify rounds and the generated client", () => {
    const state = play([
      started,
      {
        type: "verify_round",
        seq: 2,
        number: 1,
        passed: 3,
        failed: 1,
        skipped: 0,
        failures: [{ name: "test_x", message: "boom" }],
        repairs: [],
      },
      {
        type: "client",
        seq: 3,
        class_name: "ShopClient",
        lang: "python",
        named_by_llm: true,
        directory: "out",
        methods: [],
        credentials: [],
        skipped: [],
        files: [],
      },
    ]);
    expect(state.rounds).toHaveLength(1);
    expect(state.client?.class_name).toBe("ShopClient");
    expect(state.timeline.map((t) => t.kind)).toEqual(["round", "client"]);
  });
});
