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
