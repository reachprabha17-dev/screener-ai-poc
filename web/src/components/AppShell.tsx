import { Link, NavLink, Outlet } from 'react-router-dom';
import { cn } from '../ui/cn';
import { HealthBadge } from './HealthBadge';
import { IdentityMenu } from './IdentityMenu';

/**
 * Three destinations, and nothing else in the chrome.
 *
 * The Streamlit implementation carried the working context — API URL, run id,
 * position id — in a sidebar that every page read and three pages wrote. That is
 * what the URL is for: a run lives at `/runs/:id`, so a reviewer can send a
 * colleague a link to the exact screen they are looking at, the back button
 * works, and no two pages can disagree about which run is selected.
 *
 * Review is not a fourth link. It belongs to a run, is reached from one, and a
 * top-level "Review" that first asks *which* run is a question the navigation
 * should already have answered.
 */
const LINKS = [
  { to: '/requisitions', label: 'Requisitions' },
  { to: '/runs', label: 'Runs' },
  { to: '/audit', label: 'Audit' },
];

export function AppShell() {
  return (
    <>
      <header className="sticky top-0 z-40 border-b border-neutral-200 bg-white/90 backdrop-blur dark:border-neutral-800 dark:bg-neutral-900/90">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-4 px-5 py-2.5">
          <Link to="/requisitions" className="leading-tight">
            <span className="font-semibold tracking-tight">Enterprise Talent Screener</span>
            <span className="block text-xs text-neutral-500 dark:text-neutral-400">
              On-premise · no data leaves this host
            </span>
          </Link>

          <nav className="flex gap-1" aria-label="Sections">
            {LINKS.map((link) => (
              <NavLink
                key={link.to}
                to={link.to}
                className={({ isActive }) =>
                  cn(
                    'rounded-full px-3 py-1.5 text-sm font-medium transition-colors',
                    isActive
                      ? 'bg-blue-600 text-white dark:bg-blue-500 dark:text-neutral-950'
                      : 'text-neutral-600 hover:bg-neutral-100 dark:text-neutral-300 dark:hover:bg-neutral-800',
                  )
                }
              >
                {link.label}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-3">
            <HealthBadge />
            <IdentityMenu />
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-5 py-6 pb-24">
        <Outlet />
      </main>
    </>
  );
}
