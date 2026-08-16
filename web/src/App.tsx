import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { Toaster } from 'sonner';
import { AppShell } from './components/AppShell';
import { ErrorBoundary } from './components/ErrorBoundary';
import { lazy, Suspense } from 'react';
import { AuditLayout } from './pages/AuditLayout';
import { DashboardPage } from './pages/DashboardPage';
import { NewRequisitionPage } from './pages/NewRequisitionPage';
import { RequisitionPage } from './pages/RequisitionPage';
import { RequisitionsPage } from './pages/RequisitionsPage';
import { ReviewPage } from './pages/ReviewPage';
import { RunLayout } from './pages/RunLayout';
import { RunProgressPage } from './pages/RunProgressPage';
import { RunsPage } from './pages/RunsPage';
import { SessionProvider } from './session/SessionProvider';

/**
 * The audit screens are loaded on demand.
 *
 * They carry the markdown renderer that turns audit rows into sentences, and a
 * recruiter working a queue of candidates never opens them. Splitting keeps that
 * weight off the screen people are in all day, and this is the one place in the
 * app where the boundary is obvious enough to be worth drawing.
 */
const RunStoryPage = lazy(() =>
  import('./pages/audit/RunStoryPage').then((m) => ({ default: m.RunStoryPage })),
);
const DecisionRecordPage = lazy(() =>
  import('./pages/audit/DecisionRecordPage').then((m) => ({ default: m.DecisionRecordPage })),
);
const AuditSearchPage = lazy(() =>
  import('./pages/audit/AuditSearchPage').then((m) => ({ default: m.AuditSearchPage })),
);

/**
 * One `QueryClient` for the app.
 *
 * `retry: 1` rather than the default three: the API is on loopback, so a failure
 * is usually a stopped service or a rejected request, and retrying those twice
 * more only delays telling the reviewer. `staleTime` is short because a run in
 * progress genuinely changes underneath the screen — but not zero, so moving
 * between tabs does not re-fetch the same candidate list three times.
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 10_000,
      refetchOnWindowFocus: true,
    },
  },
});

export function App() {
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <SessionProvider>
          {/* Outcomes of an action the reviewer just took — "saved version 3",
              "12 files queued" — are transient and belong beside the click, not
              as a banner that pushes the page down. Anything that has to be read
              before continuing stays an inline `Alert`. */}
          <Toaster position="bottom-right" richColors closeButton />
          {/* The bundle is served from `/ui/`, so the router's base is the
              build's base — one place decides it, and dev matches production. */}
          <BrowserRouter basename={import.meta.env.BASE_URL}>
            <Routes>
              <Route element={<AppShell />}>
                <Route index element={<DashboardPage />} />

                <Route path="requisitions">
                  <Route index element={<RequisitionsPage />} />
                  <Route path="new" element={<NewRequisitionPage />} />
                  <Route path=":positionId" element={<RequisitionPage />} />
                </Route>

                <Route path="runs">
                  <Route index element={<RunsPage />} />
                  <Route path=":runId" element={<RunLayout />}>
                    <Route index element={<RunProgressPage />} />
                    <Route path="review" element={<ReviewPage />} />
                  </Route>
                </Route>

                <Route
                  path="audit"
                  element={
                    <Suspense fallback={<p className="text-sm text-neutral-500">Loading…</p>}>
                      <AuditLayout />
                    </Suspense>
                  }
                >
                  <Route index element={<RunStoryPage />} />
                  <Route path="record" element={<DecisionRecordPage />} />
                  <Route path="search" element={<AuditSearchPage />} />
                </Route>

                <Route path="*" element={<Navigate to="/" replace />} />
              </Route>
            </Routes>
          </BrowserRouter>
        </SessionProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
