import { useForm } from 'react-hook-form';
import { toast } from 'sonner';
import { useDecide } from '../api/queries';
import { DECISIONS, type CandidateSummary, type RecordableDecision } from '../api/types';
import { Alert } from '../ui/Alert';
import { Button } from '../ui/Button';
import { Choice, Field } from '../ui/Field';

interface DecisionFields {
  decision: RecordableDecision;
  reason: string;
}

/**
 * A reviewer taking responsibility for one outcome.
 *
 * The reason is required and is not a formality: it is the adverse-action record,
 * the answer to "why was I rejected", and it is stored against the reviewer's
 * name. Decisions append to a history rather than overwrite — a changed decision
 * keeps the one before it and the reason given at the time.
 *
 * `react-hook-form` owns the field state and the required-reason rule so this
 * component does not re-implement validation, touched-state and error wiring for
 * the third time in this app.
 */
export function DecisionForm({ candidate, runId }: { candidate: CandidateSummary; runId: string }) {
  const decide = useDecide(runId);
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<DecisionFields>({ defaultValues: { decision: 'advance', reason: '' } });

  if (candidate.id === null) {
    return (
      <Alert tone="warn">
        <p>
          This file produced no candidate record, so no decision can be recorded against it. It is
          listed under the run&apos;s failed files.
        </p>
      </Alert>
    );
  }

  const candidateId = candidate.id;

  const onSubmit = handleSubmit((values) => {
    decide.mutate(
      { candidateId, decision: values.decision, reason: values.reason.trim() },
      {
        onSuccess: () => {
          toast.success('Decision recorded.');
          reset({ decision: values.decision, reason: '' });
        },
        onError: (error) => {
          toast.error(error.message);
        },
      },
    );
  });

  return (
    <form onSubmit={(event) => void onSubmit(event)} className="space-y-3">
      {candidate.decision === 'undecided' ? null : (
        <Alert tone="ok">
          <p>
            Recorded as <strong>{candidate.decision}</strong> by {candidate.decided_by ?? 'someone'}
            . Recording another decision appends to the history; it does not erase this one.
          </p>
        </Alert>
      )}

      <fieldset className="space-y-1.5">
        <legend className="text-sm font-medium">Decision</legend>
        <div className="flex gap-4">
          {DECISIONS.map((option) => (
            <Choice key={option} type="radio" value={option} {...register('decision')}>
              {option}
            </Choice>
          ))}
        </div>
      </fieldset>

      <Field label="Reason">
        <textarea
          rows={3}
          placeholder="Required. Recorded against your name and shown in the candidate's record."
          {...register('reason', {
            validate: (value) =>
              value.trim().length > 0 || 'A reason is required. It is the record.',
          })}
        />
      </Field>

      {errors.reason ? (
        <Alert tone="error">
          <p>{errors.reason.message}</p>
        </Alert>
      ) : null}

      <Button type="submit" variant="primary" busy={decide.isPending}>
        Record decision
      </Button>
    </form>
  );
}
