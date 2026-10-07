import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SchemaTree, typeLabel } from "./SchemaTree";
import { describeAction } from "./Timeline";
import { AgentText, MethodBadge, plural, timeAgo } from "./ui";

afterEach(cleanup);

const schema = {
  type: "object",
  required: ["items"],
  properties: {
    items: {
      type: "array",
      items: { type: "object", properties: { id: { type: "integer" }, name: { type: ["string", "null"] } } },
    },
    page: { type: "integer" },
  },
};

describe("SchemaTree", () => {
  it("labels types", () => {
    expect(typeLabel({ type: "array", items: { type: "string" } })).toBe("array of string");
    expect(typeLabel({ anyOf: [{ type: "integer" }, { type: "null" }] })).toBe("integer | null");
  });

  it("shows top-level fields and expands nested ones", () => {
    render(<SchemaTree schema={schema} />);
    expect(screen.getByText("items")).toBeTruthy();
    expect(screen.getByText("page")).toBeTruthy();
    expect(screen.queryByText("name")).toBeNull();
    fireEvent.click(screen.getByLabelText("Expand items"));
    expect(screen.getByText("name")).toBeTruthy();
    expect(screen.getByText("string | null")).toBeTruthy();
  });
});

describe("small helpers", () => {
  it("describes agent actions", () => {
    expect(describeAction("click", { element_id: 4 })).toBe("Click element #4");
    expect(describeAction("type", { text: "tea", submit: true })).toBe('Type "tea" and press Enter');
    expect(describeAction("scroll", {})).toBe("Scroll down");
  });

  it("formats counts and times", () => {
    expect(plural(1, "endpoint")).toBe("1 endpoint");
    expect(plural(2, "endpoint")).toBe("2 endpoints");
    expect(timeAgo(new Date().toISOString())).toBe("just now");
    expect(timeAgo(new Date(Date.now() - 3 * 3600_000).toISOString())).toBe("3 h ago");
  });

  it("renders a method badge", () => {
    render(<MethodBadge method="GET" />);
    expect(screen.getByText("GET").className).toContain("text-info");
  });
});

describe("AgentText", () => {
  it("shows `backticks` as code", () => {
    const { container } = render(<AgentText text="The endpoint `/api/quotes` covers it" />);
    expect(container.querySelector("code")?.textContent).toBe("/api/quotes");
    expect(container.textContent).toBe("The endpoint /api/quotes covers it");
  });
});

describe("site ids in URLs", () => {
  it("round-trips a site with a section", async () => {
    const { siteId, siteFromId } = await import("../api");
    expect(siteId("www.google.com/maps")).toBe("www.google.com~maps");
    expect(siteFromId("www.google.com~maps")).toBe("www.google.com/maps");
    expect(siteId("hn.algolia.com")).toBe("hn.algolia.com");
  });
});

describe("Exposure", () => {
  it("shows what was hidden, masked, and a site banner", async () => {
    const { ExposureBanner, ExposurePanel } = await import("./Exposure");
    const exposure = {
      endpoint: "GET x/api/venue/{id}",
      label: "GET /api/venue/{id}",
      open: true,
      severity: "high" as const,
      kinds: ["PAN number", "email address"],
      fields: [
        { path: "data.tax_info.pan_number", kind: "pan", severity: "high" as const, example: "AB*******F", count: 2 },
        { path: "data.created_by", kind: "email", severity: "medium" as const, example: "a***@g***.com", count: 2 },
      ],
    };
    render(<ExposurePanel exposure={exposure} />);
    expect(screen.getByText("Personal data exposed without a login")).toBeTruthy();
    expect(screen.getByText("AB*******F")).toBeTruthy();
    expect(screen.getByText("data.created_by")).toBeTruthy();
    cleanup();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    render(<ExposureBanner endpoints={[{ exposure } as any, { exposure: null } as any]} />);
    expect(screen.getByRole("alert").textContent).toContain(
      "1 endpoint exposes personal data without a login: PAN number, email address",
    );
  });
});

describe("Watch changes", () => {
  it("shows new, changed (before → after) and gone", async () => {
    const { ChangeLine } = await import("../pages/Watches");
    render(
      <ul>
        <ChangeLine change={{ kind: "changed", key: "id:3", title: "Tiki Taka", fields: { avg_price: [214, 200] } }} />
        <ChangeLine change={{ kind: "new", key: "id:9", title: "New Turf", fields: {} }} />
      </ul>,
    );
    expect(screen.getByText("Tiki Taka")).toBeTruthy();
    expect(screen.getByText("avg_price").parentElement?.textContent).toBe("avg_price: 214 → 200");
    expect(screen.getByLabelText("New")).toBeTruthy();
  });
});

describe("OtherTools", () => {
  it("builds copy-paste commands for a site with a section", async () => {
    const { OtherTools } = await import("./OtherTools");
    const endpoint = {
      key: "k", label: "POST /x", method: "POST", host: "web-gateway.turftown.in", path: "/x",
      third_party: false, callable: true, operation: "get_nearby_venues",
      inputs: [{ name: "area", location: "body", type: "str", example: "velachery", required: false }],
    };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    render(<OtherTools site="turftown.in/chennai" endpoints={[endpoint as any]} />);
    const text = document.body.textContent ?? "";
    expect(text).toContain("ghost-api fetch turftown.in/chennai get_nearby_venues area=velachery --format xlsx");
    expect(text).toContain("http://127.0.0.1:8900/turftown.in~chennai/get_nearby_venues?format=csv");
    expect(text).toContain("claude mcp add ghost-turftown -- ghost-api mcp turftown.in/chennai");
  });
});
