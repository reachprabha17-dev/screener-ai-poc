import { useForm } from 'react-hook-form';
import { toast } from 'sonner';
import { useDecideBulk } from '../api/queries';
import { DECISIONS, type CandidateSummary, type RecordableDecision } from '../api/types';
import { Alert } from '../ui/Alert';
import { Button } from '../ui/Button';
import { Disclosure } from '../ui/Disclosure';
import { Choice, Field } from '../ui/Field';

interface BulkFields {
  decision: RecordableDecision;
  reason: string;
}

/**
 * One decision across a group, with a shared reason.
 *
 * Offered on the ranked groups and **not** on needs-review. A reviewer working 400
 * clear rejections one form at a time stops reading them, so the bulk path is
 * real; but the escalated ones are exactly where the system said a human has to
 * look, and the server skips them regardless of what this sends.
 */
export function BulkDecisionForm({
  candidates,
  runId,
}: {
  candidates: CandidateSummary[];
  runId: string;
}) {
  const bulk = useDecideBulk(runId);
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<BulkFields>({ defaultValues: { decision: 'reject', reason: '' } });

  const eligible = candidates.filter(
    (candidate): candidate is CandidateSummary & { id: number } =>
      !candidate.review_required && candidate.id !== null,
  );

  if (candidates.length === 0) return null;

  if (eligible.length === 0) {
    // Not nothing: every candidate here needs review, so bulk has nothing to
    // offer — but rendering nothing looks identical to the control being
    // broken. Say why, and point at the way to actually decide them (11.11).
    return (
      <Alert tone="info">
        <p>Every candidate in this group needs review — open each one to decide.</p>
      </Alert>
    );
  }

  const onSubmit = handleSubmit((values) => {
    bulk.mutate(
      {
        candidate_ids: eligible.map((candidate) => candidate.id),
        decision: values.decision,
        reason: values.reason.trim(),
      },
      {
        onSuccess: (result) => {
          toast.success(`Recorded for ${String(result.decided.length)}.`);
          if (result.skipped.length > 0) {
            toast.warning(
              `${String(result.skipped.length)} skipped — they need review and have to be opened individually.`,
            );
          }
          reset({ decision: values.decision, reason: '' });
        },
        onError: (error) => {
          toast.error(error.message);
        },
      },
    );
  });

  return (
    <Disclosure summary={`Decide on all ${String(eligible.length)} at once`}>
      <form onSubmit={(event) => void onSubmit(event)} className="space-y-3">
        <div className="flex gap-4">
          {DECISIONS.map((option) => (
            <Choice key={option} type="radio" value={option} {...register('decision')}>
              {option}
            </Choice>
          ))}
        </div>

        <Field label="Shared reason">
          <textarea
            rows={2}
            placeholder="Required. Recorded against every candidate in this group."
            {...register('reason', {
              validate: (value) =>
                value.trim().length > 0 ||
                'A reason is required. It is recorded against every one of them.',
            })}
          />
        </Field>

        {errors.reason ? (
          <Alert tone="error">
            <p>{errors.reason.message}</p>
          </Alert>
        ) : null}

        <Button type="submit" busy={bulk.isPending}>
          Record for {eligible.length}
        </Button>
      </form>
    </Disclosure>
  );
}
