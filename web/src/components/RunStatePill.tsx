import type { RunState } from '../api/types';
import { Badge } from '../ui/Badge';

const TONE = {
  pending: 'neutral',
  running: 'info',
  completed: 'ok',
  // `empty` is not a failure and not a success: the folder held nothing to
  // screen, which is a fact about the share rather than about the applicants.
  empty: 'neutral',
  failed: 'error',
  aborted: 'warn',
} as const;

export function RunStatePill({ state }: { state: RunState }) {
  return <Badge tone={TONE[state]}>{state}</Badge>;
}
