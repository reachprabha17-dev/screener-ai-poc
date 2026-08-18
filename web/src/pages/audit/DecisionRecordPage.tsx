import { useSearchParams } from 'react-router-dom';
import { everyone, useAdverseActionRecord, useCandidates } from '../../api/queries';
import type { AdverseActionRecord, CriterionAuditView, VerificationStatus } from '../../api/types';
import { QueryState } from '../../components/QueryState';
import { RunSelect } from '../../components/RunSelect';
import { dateTime, shortHash } from '../../lib/format';
import { VERDICT_LABEL } from '../../lib/labels';
import { Alert } from '../../ui/Alert';
import { Card, CardBody, CardHeader } from '../../ui/Card';
import { Disclosure } from '../../ui/Disclosure';
import { Field } from '../../ui/Field';
import { Table, Td, Th } from '../../ui/Table';

/**
 * One person's outcome, and everything that produced it.
 *
 * The artefact handed to a regulator or to the applicant. Deliberately available
 * for every decision rather than only rejections: a record that exists solely for
 * adverse outcomes cannot be checked against a favourable one, and that comparison
 * is the first test of whether it is honest.
 */
export function DecisionRecordPage() {
  const [params, setParams] = useSearchParams();
  const runId = params.get('run') ?? '';
  const candidateId = params.get('candidate');
  const candidates = useCandidates(runId);
  const record = useAdverseActionRecord(candidateId === null ? null : Number(candidateId));

  const decided = everyone(candidates.data).filter(
    (candidate) => candidate.id !== null && candidate.decision !== 'undecided',
  );

  return (
    <div className="space-y-4">
      <Card>
        <CardBody>
          <RunSelect
            value={runId}
            onChange={(next) => {
              setParams(next ? { run: next } : {});
            }}
          />

          {runId ? (
            decided.length === 0 && !candidates.isPending ? (
              <Alert tone="info">
                <p>
                  No decisions recorded for this run yet. A record exists once a reviewer has
                  advanced, held or rejected someone.
                </p>
              </Alert>
            ) : (
              <Field label="Candidate">
                <select
                  value={candidateId ?? ''}
                  onChange={(event) => {
                    setParams({ run: runId, candidate: event.target.value });
                  }}
                >
                  <option value="">Choose a candidate…</option>
                  {decided.map((candidate) => (
                    <option key={candidate.file_sha256} value={String(candidate.id)}>
                      {candidate.filename} — {candidate.decision.toUpperCase()}
                    </option>
                  ))}
                </select>
              </Field>
            )
          ) : null}
        </CardBody>
      </Card>

      {candidateId ? (
        <QueryState query={record} loading="Assembling the record…">
          {(data) => <Record record={data} />}
        </QueryState>
      ) : null}
    </div>
  );
}

function Record({ record }: { record: AdverseActionRecord }) {
  const outcome = record.decision.toUpperCase();

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title={`${record.filename} — ${outcome}`}
          hint={
            <>
              Job posting <strong>{record.position_reference}</strong> · run{' '}
              <code>{record.run_id}</code> · screened {dateTime(record.scored_at)}
            </>
          }
        />
        <CardBody>
          {outcome === 'REJECT' ? (
            <Alert tone="error">
              <p>This is an adverse outcome. The grounds below are the record of why.</p>
            </Alert>
          ) : null}

          <h3 className="font-semibold">Grounds given</h3>
          {record.history.length === 0 ? (
            <Alert tone="warn">
              <p>No stated reason is recorded for this decision.</p>
            </Alert>
          ) : (
            record.history.map((step, index) => (
              <div key={`${step.at}-${String(index)}`} className="space-y-1">
                <p className="text-sm text-neutral-500 dark:text-neutral-400">
                  <strong>{step.actor_id}</strong> changed <em>{step.from_decision}</em> →{' '}
                  <strong>{step.to_decision}</strong> on {dateTime(step.at)}
                </p>
                {/* Never truncated: this is the answer to "why was I rejected". */}
                <Alert tone="info">
                  <p>{step.reason}</p>
                </Alert>
              </div>
            ))
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Assessment against the approved rubric" />
        <CardBody>
          {record.scoreable ? (
            <p className="text-sm">
              Band <strong>{record.band ?? '—'}</strong> · score {record.score ?? '—'}/10 · hard
              requirements {record.must_haves_met ? 'met' : <strong>not met</strong>}
            </p>
          ) : (
            // None, never 0.0 — an unreadable document is not a weak applicant.
            <p className="text-sm">
              <strong>Not scored</strong> — hard requirements{' '}
              {record.must_haves_met ? 'met' : 'not met'}. See the flags below.
            </p>
          )}

          <VerificationBanner status={record.verification_status} />

          <Table caption="Criteria as assessed">
            <thead>
              <tr>
                <Th>Met?</Th>
                <Th>Criterion</Th>
                <Th>Must</Th>
                <Th>W</Th>
                {/* Three columns, three passes: what the judge first said, what
                    stood after the consistency gate, and what the second model
                    made of it. Collapsing them loses the disagreement. */}
                <Th>Judge said</Th>
                <Th>Final verdict</Th>
                <Th>Quote found</Th>
                <Th>Verifier</Th>
              </tr>
            </thead>
            <tbody>
              {record.criteria.map((criterion) => (
                <tr key={criterion.id}>
                  <Td>{VERDICT_LABEL[criterion.verdict]}</Td>
                  <Td>{criterion.text}</Td>
                  <Td>{criterion.must_have ? 'yes' : ''}</Td>
                  <Td>{criterion.weight}</Td>
                  <Td className="text-neutral-500 dark:text-neutral-400">
                    {criterion.model_verdict}
                  </Td>
                  <Td>{criterion.verdict}</Td>
                  <Td>{criterion.verified ? 'yes' : <strong>NO</strong>}</Td>
                  <Td>{verifierCell(criterion)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>

          <p className="text-sm text-neutral-500 dark:text-neutral-400">
            <strong>Judge said</strong> is the first model&apos;s verdict before the consistency
            gate; <strong>Final verdict</strong> is what stood. <strong>Quote found</strong> is
            whether the judge&apos;s evidence was located in the document. Open a criterion for the
            quote and the verifier&apos;s reasoning.
          </p>

          <div className="space-y-2">
            {record.criteria.map((criterion) => (
              <CriterionDetail key={criterion.id} criterion={criterion} />
            ))}
          </div>

          {record.flags.length > 0 ? (
            <p className="text-sm">
              <strong>Flags:</strong> {record.flags.join(', ')}
            </p>
          ) : null}
          {record.summary ? (
            <p className="text-sm">
              <strong>Summary:</strong> {record.summary}
            </p>
          ) : null}
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          title="Provenance"
          hint="What determined this outcome, captured when it was scored. The model tag will have moved and the prompt will have been edited; these will not."
        />
        <CardBody>
          <pre className="overflow-x-auto rounded-lg bg-neutral-100 p-3 font-mono text-xs dark:bg-neutral-800/60">
            {[
              `rubric              v${String(record.rubric_version ?? '?')}  ${shortHash(record.rubric_hash, 32)}…`,
              `judge model         ${shortHash(record.judge_digest, 32)}…`,
              `verifier            ${record.verifier_digest ? `${shortHash(record.verifier_digest, 32)}…` : '—'}`,
              `prompt              ${shortHash(record.prompt_hash, 32)}…`,
              `app version         ${record.app_version}`,
              `redaction           ${record.redaction_on ? 'on' : 'off'}`,
              `rubric approved by  ${record.rubric_approved_by ?? '—'}`,
              `run signed off by   ${record.run_signed_off_by ?? 'not signed off'}`,
            ].join('\n')}
          </pre>
        </CardBody>
      </Card>
    </div>
  );
}

/**
 * Say whether phase 2 ran, rather than leaving silence to be interpreted.
 *
 * A record with no verifier disagreement means one of three different things — the
 * second model agreed, it was skipped, or it has not run yet — and an auditor
 * cannot tell them apart from the criteria table alone.
 */
function VerificationBanner({ status }: { status: VerificationStatus }) {
  if (status === 'done') {
    return (
      <p className="text-sm text-neutral-500 dark:text-neutral-400">
        A second model independently checked the judge&apos;s evidence on this candidate.
      </p>
    );
  }
  if (status === 'skipped') {
    return (
      <Alert tone="warn" title="Phase 2 did not run for this candidate">
        <p>
          Verification is skipped for unscoreable candidates and for runs with it disabled, so
          nothing here has had a second opinion.
        </p>
      </Alert>
    );
  }
  return (
    <Alert tone="warn" title="Verification is still pending">
      <p>
        This assessment is provisional — the second model has not checked it, and escalations it
        would raise are not shown.
      </p>
    </Alert>
  );
}

/** One-word summary of phase 2 for the scannable table. */
function verifierCell(criterion: CriterionAuditView): string {
  if (criterion.support) {
    return { supported: 'agrees', insufficient: 'insufficient', contradicted: 'disputes' }[
      criterion.support
    ];
  }
  if (criterion.absence_confirmed === true) return 'absence confirmed';
  if (criterion.absence_confirmed === false) return 'found evidence';
  return '—';
}

/**
 * The quote, the match numbers, and the verifier's reasoning in full.
 *
 * Behind a disclosure rather than in the table because these are paragraphs; on
 * the record rather than omitted because they are the substance of the assessment.
 */
function CriterionDetail({ criterion }: { criterion: CriterionAuditView }) {
  return (
    <Disclosure
      summary={
        <span className="min-w-0 truncate">
          {criterion.id} — {criterion.text.slice(0, 70)} ({criterion.verdict}
          {criterion.must_have ? ' · must-have' : ''})
        </span>
      }
    >
      <div>
        <h4 className="font-semibold">Judge</h4>
        {criterion.evidence ? (
          <blockquote>{criterion.evidence}</blockquote>
        ) : (
          <p className="text-sm text-neutral-500 dark:text-neutral-400">
            No evidence quoted — the criterion was judged absent.
          </p>
        )}
        {criterion.model_verdict !== criterion.verdict ? (
          <Alert tone="warn">
            <p>
              The judge first said <strong>{criterion.model_verdict}</strong>; the consistency gate
              forced it to <strong>{criterion.verdict}</strong> because the quote contradicted the
              claim.
            </p>
          </Alert>
        ) : null}
        {criterion.verified ? (
          <p className="text-xs text-neutral-500 dark:text-neutral-400">
            Quote located · match ratio {criterion.match_ratio.toFixed(2)} · longest run{' '}
            {criterion.longest_span} tokens
          </p>
        ) : (
          <Alert tone="error">
            <p>
              The quoted evidence could not be matched back to the document. The verdict was left as
              returned and the candidate escalated — the system could not verify its own output.
            </p>
          </Alert>
        )}
        {criterion.negation_suspected ? (
          <Alert tone="warn">
            <p>A negation appears just before this quote — read the full sentence.</p>
          </Alert>
        ) : null}
      </div>

      <div>
        <h4 className="font-semibold">Verifier (second model)</h4>
        <VerifierVerdict criterion={criterion} />
        {criterion.verifier_rationale ? (
          <p className="text-sm italic">{criterion.verifier_rationale}</p>
        ) : null}
        {criterion.absence_evidence ? (
          // Re-verified through stage B before it was allowed to escalate, so
          // this is a quote from the document rather than a second assertion.
          <blockquote>{criterion.absence_evidence}</blockquote>
        ) : null}
      </div>
    </Disclosure>
  );
}

function VerifierVerdict({ criterion }: { criterion: CriterionAuditView }) {
  if (criterion.support === 'supported') {
    return (
      <Alert tone="ok">
        <p>Agrees: the excerpt supports the claim.</p>
      </Alert>
    );
  }
  if (criterion.support) {
    return (
      <Alert tone="error">
        <p>
          Disagrees ({criterion.support}) — would have said{' '}
          <strong>{(criterion.suggested_verdict ?? '—').toUpperCase()}</strong>.
        </p>
      </Alert>
    );
  }
  if (criterion.absence_confirmed === true) {
    return (
      <Alert tone="ok">
        <p>Agrees the criterion is genuinely absent from the document.</p>
      </Alert>
    );
  }
  if (criterion.absence_confirmed === false) {
    return (
      <Alert tone="error">
        <p>Disputes the absence — it located evidence the judge missed.</p>
      </Alert>
    );
  }
  return (
    <p className="text-sm text-neutral-500 dark:text-neutral-400">
      Not checked. See the note above the table for why.
    </p>
  );
}
