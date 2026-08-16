import { createContext, use } from 'react';
import type { Identity } from '../api/types';

export interface Session extends Identity {
  setActor: (actor: string) => void;
  setRoles: (roles: string[]) => void;
}

export const SessionContext = createContext<Session | null>(null);

/**
 * Who the reviewer says they are.
 *
 * Stubbed authentication, exactly as the API's `X-Actor` header is: the PoC uses
 * a name the operator types instead of a login screen, because the thing worth
 * demonstrating is separation of duties in the audit log — one person approving a
 * rubric and a different one signing off the run — and a single hardcoded
 * identity would make that untestable.
 */
export function useSession(): Session {
  const session = use(SessionContext);
  if (!session) throw new Error('useSession must be used inside <SessionProvider>');
  return session;
}
