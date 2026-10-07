import { useQuery } from "@tanstack/react-query";
import { Moon, Radio, Settings, Sun } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";
import { api } from "../api";
import { Logo } from "./Logo";
import { cx } from "./ui";

function useTheme() {
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
  return { dark, toggle };
}

export function Layout({ children }: { children: ReactNode }) {
  const { dark, toggle } = useTheme();
  const state = useQuery({ queryKey: ["state"], queryFn: api.state, refetchInterval: 5000 });
  const running = state.data?.running ?? null;

  const navClass = ({ isActive }: { isActive: boolean }) =>
    cx(
      "rounded-lg px-2.5 py-1.5 text-sm font-medium transition",
      isActive ? "bg-surface-2 text-fg" : "text-muted hover:text-fg",
    );

  return (
    <div className="flex min-h-screen flex-col">
      <header className="sticky top-0 z-20 border-b border-line bg-bg/80 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-[1440px] items-center gap-3 px-4">
          <Link to="/" aria-label="Ghost API home">
            <Logo />
          </Link>
          <nav className="ml-2 flex items-center gap-1">
            <NavLink to="/" end className={navClass}>
              Sites
            </NavLink>
          </nav>
          <div className="ml-auto flex items-center gap-1.5">
            {running !== null && (
              <Link
                to={`/runs/${running}`}
                className="flex items-center gap-1.5 rounded-full bg-accent-soft px-2.5 py-1 text-xs font-medium text-accent"
              >
                <Radio className="size-3.5 animate-pulse" aria-hidden />
                <span className="hidden sm:inline">Run #{running} live</span>
                <span className="sm:hidden">Live</span>
              </Link>
            )}
            <NavLink to="/settings" className={navClass} aria-label="Settings">
              <Settings className="size-4" />
            </NavLink>
            <button
              type="button"
              onClick={toggle}
              aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
              className="rounded-lg p-2 text-muted transition hover:bg-surface-2 hover:text-fg"
            >
              {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
            </button>
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-[1440px] flex-1 px-4 py-6">{children}</main>
      <footer className="border-t border-line py-4 text-center text-xs text-muted">
        Ghost API {state.data?.version ?? ""} · runs locally on your computer · nothing is sent anywhere
        except the sites you scan and your LLM provider
      </footer>
    </div>
  );
}
