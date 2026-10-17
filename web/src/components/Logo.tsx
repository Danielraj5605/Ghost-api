export function GhostMark({ className = "size-7" }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={`text-accent ${className}`} aria-hidden>
      <path
        d="M16 3C9.9 3 6 7.6 6 13.5V27c0 .9 1 1.4 1.7.8l2.3-1.9 2.4 2a1 1 0 0 0 1.3 0l2.3-2 2.3 2a1 1 0 0 0 1.3 0l2.4-2 2.3 1.9c.7.6 1.7.1 1.7-.8V13.5C26 7.6 22.1 3 16 3Z"
        fill="currentColor"
      />
      <circle cx="12.3" cy="14" r="2" fill="var(--bg)" />
      <circle cx="19.7" cy="14" r="2" fill="var(--bg)" />
    </svg>
  );
}

export function Logo() {
  return (
    <span className="flex items-center gap-2 font-semibold tracking-tight">
      <GhostMark />
      <span>
        Ghost <span className="text-accent">API</span>
      </span>
    </span>
  );
}
