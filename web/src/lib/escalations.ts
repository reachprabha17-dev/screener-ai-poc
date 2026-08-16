/**
 * Escalation reasons counted across a group of candidates.
 *
 * The run status endpoint returns this breakdown ready-made while a run is in
 * flight; the review screen has only the candidate list, so it derives the same
 * shape here rather than showing one undifferentiated count.
 */
export function countEscalations(reasons: string[][]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const list of reasons) {
    for (const reason of list) counts[reason] = (counts[reason] ?? 0) + 1;
  }
  return counts;
}
