import { useCallback, useMemo, useState, type ReactNode } from 'react';
import { SessionContext, type Session } from './context';

const ACTOR_KEY = 'screener.actor';
const ROLES_KEY = 'screener.roles';

const DEFAULT_ACTOR = 'poc-operator';
/**
 * `auditor` is not granted by default.
 *
 * The audit reads — the log, a run's story, a candidate's record — are the only
 * role-gated surface in the system, and a default that quietly includes the role
 * turns the gate into something nobody has ever seen work. An operator adds it
 * deliberately, which is also how they discover it exists.
 */
const DEFAULT_ROLES = ['admin'];

/**
 * Read a stored preference, checking it is still the shape this build expects.
 *
 * `localStorage` outlives deploys, so what comes back was written by whatever
 * version of this app the reviewer last had open. An unvalidated `JSON.parse`
 * here puts an arbitrary value into the actor header on every request — and the
 * actor header is what the audit log records against a decision.
 */
function readStored<T>(key: string, fallback: T, valid: (value: unknown) => value is T): T {
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) return fallback;
    const parsed: unknown = JSON.parse(raw);
    return valid(parsed) ? parsed : fallback;
  } catch {
    // Private-mode, or a value written by an older build. Neither is worth an
    // error screen over a preference.
    return fallback;
  }
}

function isName(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}

function isRoleList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((role) => typeof role === 'string');
}

function store(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Nothing here is candidate data; losing it costs one retyped name.
  }
}

/**
 * Identity for the session, remembered across reloads.
 *
 * `localStorage` and not a cookie or the URL: it holds a display name and a role
 * list, it is per-browser rather than per-tab so a reviewer's name survives the
 * reload that a deploy causes, and nothing in it is candidate data.
 */
export function SessionProvider({ children }: { children: ReactNode }) {
  const [actor, setActorState] = useState(() => readStored(ACTOR_KEY, DEFAULT_ACTOR, isName));
  const [roles, setRolesState] = useState(() => readStored(ROLES_KEY, DEFAULT_ROLES, isRoleList));

  const setActor = useCallback((next: string) => {
    setActorState(next);
    store(ACTOR_KEY, next);
  }, []);

  const setRoles = useCallback((next: string[]) => {
    setRolesState(next);
    store(ROLES_KEY, next);
  }, []);

  const value = useMemo<Session>(
    () => ({ actor, roles, setActor, setRoles }),
    [actor, roles, setActor, setRoles],
  );

  return <SessionContext value={value}>{children}</SessionContext>;
}
