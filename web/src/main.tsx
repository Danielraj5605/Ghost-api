import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { EmptyState, Skeleton } from "./components/ui";
import "./index.css";
import { Home } from "./pages/Home";

// Heavier pages (code highlighting, Markdown) load on first visit.
const Run = lazy(() => import("./pages/Run").then((m) => ({ default: m.Run })));
const Site = lazy(() => import("./pages/Site").then((m) => ({ default: m.Site })));
const Watches = lazy(() => import("./pages/Watches").then((m) => ({ default: m.Watches })));
const Settings = lazy(() => import("./pages/Settings").then((m) => ({ default: m.Settings })));

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 5_000, retry: 1, refetchOnWindowFocus: true } },
});

function NotFound() {
  return (
    <EmptyState title="Page not found">
      <Link to="/" className="text-accent hover:underline">
        Back to your sites
      </Link>
    </EmptyState>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Layout>
          <Suspense fallback={<Skeleton className="h-96" />}>
            <Routes>
              <Route path="/" element={<Home />} />
              <Route path="/runs/:id" element={<Run />} />
              <Route path="/sites/:domain" element={<Site />} />
              <Route path="/watches" element={<Watches />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="*" element={<NotFound />} />
            </Routes>
          </Suspense>
        </Layout>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
