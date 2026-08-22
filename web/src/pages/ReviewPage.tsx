import { Download } from 'lucide-react';
import { useParams, useSearchParams } from 'react-router-dom';
import { toast } from 'sonner';
import { useCandidates, useSignOff } from '../api/queries';
import type { CandidateSummary, RankedCandidates } from '../api/types';
import { BandPill } from '../components/BandPill';
import { BulkDecisionForm } from '../components/BulkDecisionForm';
import { CandidateDetail } from '../components/CandidateDetail';
import { EscalationMeter } from '../components/EscalationMeter';
import { QueryState } from '../components/QueryState';
import { countEscalations } from '../lib/escalations';
import { downloadCsv, toCsv } from '../lib/format';
import { VERIFICATION_BADGE } from '../lib/labels';
import { Alert } from '../ui/Alert';
import { Badge } from '../ui/Badge';
import { Button } from '../ui/Button';
import { Card, CardBody, CardHeader } from '../ui/Card';
import { cn } from '../ui/cn';
import { Disclosure } from '../ui/Disclosure';
import { Table, Td, Th } from '../ui/Table';

/**
 * The results, in three groups that are never ranked against each other.
 *
 * **`needs_review` is its own group, first, and open.** At 1,000 applicants a
 * reviewer reads the top of Band A and stops. An unscoreable candidate parked at
 * the bottom of one long list is invisible in practice — which is the adverse
 * outcome escalation was redesigned to prevent (10.5b).
 *
 * **Missing a must-have is a separate group, not a low score.** Those candidates
 * are ranked among themselves and never against the ones who met the hard
 * requirements; the two are not comparable, and a single list implies they are.
 */
export function ReviewPage() {
  const { runId = '' } = useParams();
  const candidates = useCandidates(runId);

  return (
    <QueryState query={candidates} loading="Loading results…">
      {(result) => <Review runId={runId} result={result} />}
    </QueryState>
  );
}

function Review({ runId, result }: { runId: string; result: RankedCandidates }) {
  const [params, setParams] = useSearchParams();
  const selectedHash = params.get('c') ?? '';

  const groups = [
    {
      key: 'needs-review',
      title: 'Needs review',
      candidates: result.needs_review,
      unranked: true,
      note: 'The system could not produce a reliable result for these. They are not ranked and not scored — that is a statement about the system, not about the candidate.',
    },
    {
      key: 'qualified',
      title: 'Meets all must-haves',
      candidates: result.meets_must_haves,
      unranked: false,
      note: '',
    },
    {
      key: 'unqualified',
      title: 'Missing a must-have',
      candidates: result.missing_must_have,
      unranked: false,
      note: 'These did not meet a stated hard requirement. They are ranked among themselves, never against the group above — the two are not comparable.',
    },
  ];

  const everyone = groups.flatMap((group) => group.candidates);
  const selected = everyone.find((candidate) => candidate.file_sha256 === selectedHash);

  function select(hash: string): void {
    const next = new URLSearchParams(params);
    next.set('c', hash);
    // `replace` so browsing candidates does not bury the run page under fifty
    // history entries a reviewer has to click back through.
    setParams(next, { replace: true });
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardBody>
          <EscalationMeter
            rate={result.escalation_rate}
            count={result.needs_review.length}
            total={everyone.length}
            breakdown={countEscalations(result.needs_review.map((c) => c.escalation_reasons))}
          />
        </CardBody>
      </Card>

      <div className="grid gap-4 lg:grid-cols-[minmax(300px,380px)_1fr]">
        <div className="space-y-3">
          {groups.map((group) => (
            <Disclosure
              key={group.key}
              // Needs-review open by default, and the qualified group only when
              // there is nothing escalated to work first.
              defaultOpen={group.key === 'needs-review' || result.needs_review.length === 0}
              summary={
                <span className="flex items-center gap-2">
                  {group.title}
                  <Badge tone={group.key === 'needs-review' ? 'warn' : 'neutral'}>
                    {group.candidates.length}
                  </Badge>
                </span>
              }
            >
              {group.note ? (
                <p className="text-sm text-neutral-500 dark:text-neutral-400">{group.note}</p>
              ) : null}

              <CandidateTable
                candidates={group.candidates}
                selectedHash={selectedHash}
                onSelect={select}
              />

              {group.candidates.length > 0 ? (
                <Button
                  size="sm"
                  title="Includes the numeric score for audit. On screen, reviewers see bands."
                  onClick={() => {
                    downloadCsv(`${runId}-${group.key}.csv`, exportCsv(group.candidates));
                  }}
                >
                  <Download className="size-3.5" aria-hidden />
                  Export CSV
                </Button>
              ) : null}

              {group.unranked ? null : (
                <BulkDecisionForm candidates={group.candidates} runId={runId} />
              )}
            </Disclosure>
          ))}
        </div>

        <Card>
          <CardBody>
            {selected ? (
              <CandidateDetail candidate={selected} runId={runId} />
            ) : (
              <p className="py-12 text-center text-sm text-neutral-500 dark:text-neutral-400">
                {everyone.length === 0
                  ? 'No results yet. Start the run from the Progress tab.'
                  : 'Choose a candidate to read their assessment and record a decision.'}
              </p>
            )}
          </CardBody>
        </Card>
      </div>

      <SignOff runId={runId} result={result} />
    </div>
  );
}

function CandidateTable({
  candidates,
  selectedHash,
  onSelect,
}: {
  candidates: CandidateSummary[];
  selectedHash: string;
  onSelect: (hash: string) => void;
}) {
  if (candidates.length === 0) {
    return (
      <p className="py-4 text-center text-sm text-neutral-500 dark:text-neutral-400">
        Nobody in this group.
      </p>
    );
  }

  return (
    <Table caption="Candidates in this group">
      <thead>
        <tr>
          <Th className="w-10">Band</Th>
          <Th>Candidate</Th>
          <Th className="w-24">Decision</Th>
        </tr>
      </thead>
      <tbody>
        {candidates.map((candidate) => (
          <tr
            key={candidate.file_sha256}
            // Selection is marked on the row and announced on the name: a
            // highlight alone is invisible to anyone using a screen reader.
            aria-current={candidate.file_sha256 === selectedHash ? 'true' : undefined}
            className={cn(
              'hover:bg-neutral-50 dark:hover:bg-neutral-800/50',
              candidate.file_sha256 === selectedHash && 'bg-blue-50 dark:bg-blue-950/40',
            )}
          >
            <Td>
              <BandPill band={candidate.band} />
            </Td>
            <Td>
              <button
                type="button"
                className="text-left font-medium hover:underline"
                onClick={() => {
                  onSelect(candidate.file_sha256);
                }}
              >
                {candidate.filename}
              </button>
              <span className="block text-xs text-neutral-500 dark:text-neutral-400">
                {VERIFICATION_BADGE[candidate.verification_status]}
                {candidate.flags.length > 0 ? ` · ${candidate.flags.join(', ')}` : ''}
              </span>
            </Td>
            <Td>
              {candidate.decision === 'undecided' ? (
                <span className="text-xs text-neutral-500 dark:text-neutral-400">undecided</span>
              ) : (
                <Badge tone="ok">{candidate.decision}</Badge>
              )}
            </Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

/**
 * Sign-off, gated on the same condition the server enforces.
 *
 * Mirroring the precondition rather than counting the visible rows means the
 * warning and the refusal agree. A screen that says "ready" and a button that
 * comes back 400 teaches reviewers to distrust the screen.
 */
function SignOff({ runId, result }: { runId: string; result: RankedCandidates }) {
  const signOff = useSignOff(runId);
  const outstanding = [
    ...result.needs_review,
    ...result.meets_must_haves,
    ...result.missing_must_have,
  ].filter(
    (candidate) =>
      candidate.decision === 'undecided' &&
      (candidate.review_required || candidate.verification_status === 'pending'),
  );

  return (
    <Card>
      <CardHeader
        title="Sign off"
        hint="A named human accepting these results. Recorded in the audit log against your name."
      />
      <CardBody>
        {outstanding.length > 0 ? (
          <Alert tone="warn">
            <p>
              {outstanding.length} candidate(s) still need a decision. Sign-off is blocked until
              each one is advanced, held or rejected — signing off with an unread queue is the
              failure this screen exists to prevent.
            </p>
          </Alert>
        ) : null}

        <Button
          variant="primary"
          disabled={outstanding.length > 0}
          busy={signOff.isPending}
          onClick={() => {
            signOff.mutate(undefined, {
              onSuccess: () => {
                toast.success('Signed off. Recorded against your name.');
              },
              onError: (error) => {
                toast.error(error.message);
              },
            });
          }}
        >
          Sign off this run
        </Button>
      </CardBody>
    </Card>
  );
}

/** The export carries the numeric score and the decision — 15.6: the outcome and
 * who owns it, not only the ranking. */
function exportCsv(candidates: CandidateSummary[]): string {
  return toCsv(
    candidates.map((c) => ({
      filename: c.filename,
      file_sha256: c.file_sha256,
      band: c.band,
      score: c.score,
      must_haves_met: c.must_haves_met,
      scoreable: c.scoreable,
      review_required: c.review_required,
      decision: c.decision,
      decided_by: c.decided_by ?? '',
      decided_at: c.decided_at ?? '',
      verification_status: c.verification_status,
      escalation_reasons: c.escalation_reasons.join('|'),
      flags: c.flags.join('|'),
      scored_at: c.scored_at,
    })),
  );
}
