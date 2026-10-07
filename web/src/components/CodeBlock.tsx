import hljs from "highlight.js/lib/core";
import json from "highlight.js/lib/languages/json";
import python from "highlight.js/lib/languages/python";
import typescript from "highlight.js/lib/languages/typescript";
import yaml from "highlight.js/lib/languages/yaml";
import { Check, Copy } from "lucide-react";
import { useMemo, useState } from "react";
import { cx } from "./ui";

hljs.registerLanguage("python", python);
hljs.registerLanguage("typescript", typescript);
hljs.registerLanguage("json", json);
hljs.registerLanguage("yaml", yaml);

export function languageFor(name: string): string {
  if (name.endsWith(".py")) return "python";
  if (name.endsWith(".ts")) return "typescript";
  if (name.endsWith(".json")) return "json";
  if (name.endsWith(".yaml") || name.endsWith(".yml")) return "yaml";
  return "plaintext";
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked */
    }
  };
  return (
    <button
      type="button"
      onClick={() => void copy()}
      className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted transition hover:bg-surface-2 hover:text-fg"
      aria-label={copied ? "Copied" : label}
    >
      {copied ? <Check className="size-3.5 text-ok" /> : <Copy className="size-3.5" />}
      <span aria-live="polite">{copied ? "Copied" : label}</span>
    </button>
  );
}

export function CodeBlock({
  code,
  language,
  title,
  className,
  maxHeight = "32rem",
}: {
  code: string;
  language: string;
  title?: string;
  className?: string;
  maxHeight?: string;
}) {
  const html = useMemo(() => {
    if (language === "plaintext" || !hljs.getLanguage(language)) return null;
    try {
      return hljs.highlight(code, { language }).value;
    } catch {
      return null;
    }
  }, [code, language]);

  return (
    <div className={cx("overflow-hidden rounded-xl border border-line bg-surface-2/50", className)}>
      <div className="flex items-center justify-between border-b border-line px-3 py-1.5">
        <span className="truncate font-mono text-xs text-muted">{title ?? language}</span>
        <CopyButton text={code} />
      </div>
      <pre className="overflow-auto p-3 font-mono text-[12.5px] leading-relaxed" style={{ maxHeight }}>
        {html !== null ? (
          <code className="hljs" dangerouslySetInnerHTML={{ __html: html }} />
        ) : (
          <code>{code}</code>
        )}
      </pre>
    </div>
  );
}
