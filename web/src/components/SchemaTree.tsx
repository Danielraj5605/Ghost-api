import { ChevronRight } from "lucide-react";
import { useState } from "react";
import type { JsonSchema } from "../types";
import { cx } from "./ui";

/** "string", "integer | null", "array of object"... */
export function typeLabel(schema: JsonSchema): string {
  if (schema.anyOf?.length) return schema.anyOf.map(typeLabel).join(" | ");
  const t = Array.isArray(schema.type) ? schema.type.join(" | ") : (schema.type ?? "any");
  if (t === "array" && schema.items) return `array of ${typeLabel(schema.items)}`;
  return t;
}

/** The object whose fields should be listed under this node, if any. */
function childrenOf(schema: JsonSchema): JsonSchema | null {
  if (schema.properties) return schema;
  if (schema.items) return childrenOf(schema.items);
  for (const option of schema.anyOf ?? []) {
    const found = childrenOf(option);
    if (found) return found;
  }
  return null;
}

function Node({
  name,
  schema,
  required,
  depth,
}: {
  name: string;
  schema: JsonSchema;
  required: boolean;
  depth: number;
}) {
  const inner = childrenOf(schema);
  const [open, setOpen] = useState(depth < 1);
  const fields = Object.entries(inner?.properties ?? {});

  return (
    <li>
      <div className="flex items-center gap-1.5 py-0.5" style={{ paddingLeft: `${depth * 14}px` }}>
        {fields.length ? (
          <button
            type="button"
            onClick={() => setOpen(!open)}
            aria-expanded={open}
            aria-label={`${open ? "Collapse" : "Expand"} ${name}`}
            className="rounded p-0.5 text-muted hover:bg-surface-2 hover:text-fg"
          >
            <ChevronRight className={cx("size-3.5 transition", open && "rotate-90")} />
          </button>
        ) : (
          <span className="w-[18px]" />
        )}
        <span className="font-mono text-[13px]">{name}</span>
        {!required && <span className="text-xs text-muted">optional</span>}
        <span className="ml-auto truncate pl-2 font-mono text-xs text-accent">{typeLabel(schema)}</span>
      </div>
      {open && fields.length > 0 && (
        <ul>
          {fields.map(([child, s]) => (
            <Node
              key={child}
              name={child}
              schema={s}
              required={inner?.required?.includes(child) ?? false}
              depth={depth + 1}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

export function SchemaTree({ schema, rootName = "response" }: { schema: JsonSchema; rootName?: string }) {
  return (
    <ul className="rounded-lg border border-line bg-surface-2/40 p-2" aria-label={`${rootName} fields`}>
      <Node name={rootName} schema={schema} required depth={0} />
    </ul>
  );
}
