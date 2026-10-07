import { Moon, Sun } from "lucide-react";
import { useState } from "react";

/** Dark/light switch; the choice is remembered in this browser when storage allows. */
export function ThemeToggle() {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark"));
  const toggle = () => {
    const next = !dark;
    document.documentElement.classList.toggle("dark", next);
    try {
      localStorage.setItem("ghost-theme", next ? "dark" : "light");
    } catch {
      /* storage blocked: the choice lasts for this page only */
    }
    setDark(next);
  };
  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
      className="rounded-lg p-2 text-muted transition hover:bg-surface-2 hover:text-fg"
    >
      {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
    </button>
  );
}
