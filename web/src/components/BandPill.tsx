import type { Band } from '../api/types';
import { BAND_HELP } from '../lib/labels';
import { cn } from '../ui/cn';

const TONE: Record<Band, string> = {
  A: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200',
  B: 'bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-200',
  C: 'bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200',
  D: 'bg-neutral-200 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-400',
};

/**
 * Reviewers see a band, not a score.
 *
 * Three verdict levels across at most twelve criteria cannot support a rendered
 * precision of `7.8`. The decimal implies resolution that does not exist and
 * invites over-reliance on a number the system cannot justify to that precision
 * (10.6).
 */
export function BandPill({ band }: { band: Band | null }) {
  return (
    <span
      className={cn(
        'inline-flex size-7 shrink-0 items-center justify-center rounded-lg text-sm font-bold',
        band === null
          ? 'bg-neutral-100 text-neutral-400 dark:bg-neutral-800 dark:text-neutral-500'
          : TONE[band],
      )}
      title={band === null ? 'Not scoreable — see the flags on this candidate' : BAND_HELP[band]}
    >
      {band ?? '—'}
    </span>
  );
}
