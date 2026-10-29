import { useQuery } from "@tanstack/react-query";
import { Eye, LayoutGrid, Radio, Settings } from "lucide-react";
import type { ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";
import { api } from "../api";
import { Logo } from "./Logo";
import { ThemeToggle } from "./ThemeToggle";
import { cx } from "./ui";

export function Layout({ children }: { children: ReactNode }) {
  const state = useQuery({ queryKey: ["state"], queryFn: api.state, refetchInterval: 5000 });
  const running = state.data?.running ?? null;

  const navClass = ({ isActive }: { isActive: boolean }) =>
    cx(
      "relative flex items-center gap-2.5 rounded-xl px-3 py-2 text-sm font-medium transition",
      isActive
        ? "bg-accent-soft text-accent before:absolute before:-left-3 before:top-2 before:bottom-2 before:hidden before:w-1 before:rounded-full before:bg-accent lg:before:block"
        : "text-muted hover:bg-surface-2 hover:text-fg",
    );

  const live = running !== null && (
    <Link
      to={`/runs/${running}`}
      className="flex items-center gap-1.5 rounded-full bg-accent-soft px-2.5 py-1 text-xs font-medium text-accent"
    >
      <Radio className="size-3.5 animate-pulse" aria-hidden />
      Run #{running} live
    </Link>
  );

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[15rem_minmax(0,1fr)]">
      <aside className="hidden border-r border-line bg-surface lg:sticky lg:top-0 lg:flex lg:h-screen lg:flex-col lg:gap-6 lg:px-6 lg:py-6">
        <Link to="/" aria-label="Ghost API home" className="px-1">
          <Logo />
        </Link>
        <nav className="flex flex-col gap-1" aria-label="Main">
          <p className="mb-1 px-3 text-[11px] font-semibold tracking-wider text-muted uppercase">Menu</p>
          <NavLink to="/" end className={navClass}>
            <LayoutGrid className="size-4" aria-hidden />
            Sites
          </NavLink>
          <NavLink to="/watches" className={navClass}>
            <Eye className="size-4" aria-hidden />
            Watches
          </NavLink>
          <p className="mt-4 mb-1 px-3 text-[11px] font-semibold tracking-wider text-muted uppercase">General</p>
          <NavLink to="/settings" className={navClass}>
            <Settings className="size-4" aria-hidden />
            Settings
          </NavLink>
        </nav>
        <div className="mt-auto flex flex-col items-start gap-3">
          {live}
          <ThemeToggle />
        </div>
      </aside>

      <div className="flex min-w-0 flex-col">
        <header className="sticky top-0 z-20 border-b border-line bg-bg/85 backdrop-blur lg:hidden">
          <div className="flex h-14 items-center gap-2 px-4">
            <Link to="/" aria-label="Ghost API home">
              <Logo />
            </Link>
            <nav className="ml-2 flex items-center gap-1" aria-label="Main">
              <NavLink to="/" end className={navClass}>
                Sites
              </NavLink>
              <NavLink to="/watches" className={navClass}>
                Watches
              </NavLink>
            </nav>
            <div className="ml-auto flex items-center gap-1.5">
              {live}
              <NavLink to="/settings" className={navClass} aria-label="Settings">
                <Settings className="size-4" />
              </NavLink>
              <ThemeToggle />
            </div>
          </div>
        </header>
        <main className="mx-auto w-full max-w-[1280px] flex-1 px-4 py-6 lg:px-8 lg:py-8">{children}</main>
        <footer className="border-t border-line px-4 py-4 text-center text-xs text-muted">
          Ghost API {state.data?.version ?? ""} · runs locally on your computer · nothing is sent anywhere
          except the sites you scan, your LLM provider and notifications you set up
        </footer>
      </div>
    </div>
  );
}
