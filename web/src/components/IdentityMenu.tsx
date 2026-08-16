import { UserRound } from 'lucide-react';
import { useSession } from '../session/context';
import { Choice, Field } from '../ui/Field';
import { Popover } from '../ui/Popover';

const ROLES = ['admin', 'auditor'] as const;

/**
 * Who you are reviewing as, and with which roles.
 *
 * Kept one click away rather than buried in a settings screen for two reasons.
 * The name is written into the audit log against every decision, so a reviewer
 * working under somebody else's name should be obvious rather than discoverable.
 * And the separation-of-duties flow — one person approves a rubric, a different
 * one signs off the run — is demonstrated by changing it, which nobody does if it
 * takes three clicks to find.
 */
export function IdentityMenu() {
  const { actor, roles, setActor, setRoles } = useSession();

  function toggleRole(role: string, on: boolean): void {
    setRoles(on ? [...new Set([...roles, role])] : roles.filter((r) => r !== role));
  }

  return (
    <Popover
      trigger={
        <button
          type="button"
          className="flex items-center gap-2 rounded-lg px-2 py-1 text-sm hover:bg-neutral-100 dark:hover:bg-neutral-800"
        >
          <UserRound className="size-4" aria-hidden />
          <span className="font-medium">{actor || 'nobody'}</span>
        </button>
      }
    >
      <div className="space-y-4">
        <Field
          label="Reviewing as"
          hint="The PoC uses this instead of a login screen. Change it after approving a rubric to see separation of duties enforced."
        >
          <input
            value={actor}
            onChange={(event) => {
              setActor(event.target.value);
            }}
            autoComplete="off"
            spellCheck={false}
          />
        </Field>

        <fieldset className="space-y-2">
          <legend className="text-sm font-medium">Roles</legend>
          <div className="flex gap-4">
            {ROLES.map((role) => (
              <Choice
                key={role}
                type="checkbox"
                checked={roles.includes(role)}
                onChange={(event) => {
                  toggleRole(role, event.target.checked);
                }}
              >
                {role}
              </Choice>
            ))}
          </div>
          <p className="text-xs text-neutral-500 dark:text-neutral-400">
            <code>auditor</code> is required to read the audit log, a run&apos;s story, and a
            candidate&apos;s record.
          </p>
        </fieldset>
      </div>
    </Popover>
  );
}
