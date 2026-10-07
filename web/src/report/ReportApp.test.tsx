import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { readReportData, ReportApp, type ReportData } from "./ReportApp";

afterEach(() => {
  cleanup();
  window.location.hash = "";
});

const data: ReportData = {
  version: "0.1.0",
  generated_at: "2026-10-07T09:00:00+00:00",
  domain: "shop.test",
  run: {
    id: 3,
    domain: "shop.test",
    start_url: "https://shop.test/",
    goal: "list products",
    status: "goal-covered",
    started_at: "2026-10-07T08:59:00+00:00",
    finished_at: "2026-10-07T08:59:42+00:00",
    requests: 12,
    llm_calls: 4,
    tokens: 900,
  },
  endpoints: [],
  events: [{ type: "notice", seq: 1, text: "Testing 2 endpoint(s)", level: "info", detail: "" }],
  lessons: ["Products load after scrolling"],
  files: [{ name: "shop_client.py", size: 20, kind: "client", content: "class ShopClient: ..." }],
  report_md: "# Ghost API report: shop.test",
};

function page(json: string): Document {
  const doc = document.implementation.createHTMLDocument();
  doc.body.innerHTML = `<script id="ghost-report" type="application/json">${json}</script>`;
  return doc;
}

describe("report viewer", () => {
  it("reads the embedded data, and nothing from the empty template", () => {
    expect(readReportData(page(JSON.stringify(data)))?.domain).toBe("shop.test");
    expect(readReportData(page("__GHOST_REPORT__"))).toBeNull();
    expect(readReportData(page("{broken"))).toBeNull();
  });

  it("shows the run and switches tabs", () => {
    render(<ReportApp data={data} />);
    expect(screen.getByRole("heading", { name: "shop.test" })).toBeTruthy();
    expect(screen.getByText("42 s")).toBeTruthy();
    expect(screen.getByText("No endpoints were found")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /Activity/ }));
    expect(screen.getByText("Testing 2 endpoint(s)")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /Lessons/ }));
    expect(screen.getByText("Products load after scrolling")).toBeTruthy();
    expect(window.location.hash).toBe("#lessons");
  });

  it("explains an unfilled template", () => {
    render(<ReportApp data={null} />);
    expect(screen.getByText("This report has no data")).toBeTruthy();
  });
});
