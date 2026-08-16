import { NavLink, Outlet } from 'react-router-dom';
import { useSession } from '../session/context';
import { Alert } from '../ui/Alert';
import { cn } from '../ui/cn';

/**
 * The audit log, as a narrative rather than a table (15.4).
 *
 * **Scoped to one run by default, searchable second.** The question people bring
 * to an audit log is "show me how this hiring decision was made and prove a human
 * was involved", and that question is always about one thing. A chronological dump
 * of every row answers a different and rarer question — "what has this person done
 * across everything" — which is what the search view is for.
 */
export function AuditLayout() {
  const { roles } = useSession();

  const tab = ({ isActive }: { isActive: boolean }) =>
    cn(
      '-mb-px border-b-2 px-4 py-2 text-sm font-medium',
      isActive
        ? 'border-blue-600 text-neutral-900 dark:border-blue-400 dark:text-neutral-50'
        : 'border-transparent text-neutral-500 hover:text-neutral-800 dark:text-neutral-400 dark:hover:text-neutral-200',
    );

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold tracking-tight">Audit</h1>
      <p className="text-sm text-neutral-500 dark:text-neutral-400">
        An append-only record of every decision and mutation, enforced by database triggers rather
        than by convention. That is the property that makes it evidence.
      </p>

      {roles.includes('auditor') ? null : (
        <Alert tone="warn" title="These reads need the auditor role">
          <p>
            The audit log names who did what and carries decision reasons, so it is gated. Add{' '}
            <code>auditor</code> to your roles under your name, top right.
          </p>
        </Alert>
      )}

      <nav
        className="flex border-b border-neutral-200 dark:border-neutral-800"
        aria-label="Audit views"
      >
        <NavLink to="." end className={tab}>
          Run story
        </NavLink>
        <NavLink to="record" className={tab}>
          Decision record
        </NavLink>
        <NavLink to="search" className={tab}>
          Search the log
        </NavLink>
      </nav>

      <Outlet />
    </div>
  );
}
