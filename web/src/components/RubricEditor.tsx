import { Trash2 } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { useApproveRubric, useExtractRubric, useSaveRubric } from '../api/queries';
import type { Criterion, Rubric } from '../api/types';
import { shortHash } from '../lib/format';
import { Alert } from '../ui/Alert';
import { Button } from '../ui/Button';
import { Card, CardBody, CardHeader } from '../ui/Card';
import { Table, Td, Th } from '../ui/Table';

const MIN_CRITERIA = 4;
const MAX_CRITERIA = 12;
const WEIGHTS = [1, 2, 3, 4, 5];

/**
 * The next free `C{n}`, from the highest one in use rather than from the count.
 *
 * Counting collides the moment a criterion is removed from the middle: delete C2
 * from four and the next added row is also called C4. That is two rows sharing a
 * React key — which React resolves by reusing the wrong DOM node, so the text a
 * reviewer typed appears against the other criterion — and two criteria sharing
 * an id in what gets saved.
 */
function nextCriterionId(criteria: Criterion[]): string {
  const highest = criteria.reduce((max, criterion) => {
    const parsed = Number(/^C(\d+)$/.exec(criterion.id)?.[1] ?? 0);
    return Math.max(max, parsed);
  }, 0);
  return `C${String(highest + 1)}`;
}

/**
 * The rubric: drafted by the model, then **edited and approved by a person**.
 *
 * Nothing is screened against an unapproved rubric. An invented requirement would
 * silently reject every applicant who lacks something the job never asked for,
 * and it would do so consistently enough to look like a working system.
 *
 * Editing is local until **Save as new version**, and saving never edits in place:
 * a run records the rubric it used and candidates carry its hash, so rewriting a
 * rubric already scored against would leave stored decisions unexplainable — a
 * reviewer reading verdicts against criteria that no longer exist.
 *
 * Mount with `key={rubric.id}` so a saved version replaces the editor's contents
 * rather than merging into them.
 */
export function RubricEditor({ rubric }: { rubric: Rubric }) {
  const [criteria, setCriteria] = useState<Criterion[]>(rubric.criteria);
  const [problem, setProblem] = useState('');
  const save = useSaveRubric(rubric.position_id);
  const approve = useApproveRubric(rubric.position_id);
  const extract = useExtractRubric(rubric.position_id);

  const approved = rubric.approved_at !== null;
  const dirty = JSON.stringify(criteria) !== JSON.stringify(rubric.criteria);

  function update(index: number, patch: Partial<Criterion>): void {
    setCriteria((current) => current.map((c, i) => (i === index ? { ...c, ...patch } : c)));
  }

  function onSave(): void {
    if (criteria.length < MIN_CRITERIA || criteria.length > MAX_CRITERIA) {
      setProblem(`A rubric holds between ${MIN_CRITERIA} and ${MAX_CRITERIA} criteria.`);
      return;
    }
    if (criteria.some((c) => !c.text.trim())) {
      setProblem('Every criterion needs text, or remove the empty row.');
      return;
    }
    setProblem('');
    save.mutate(
      { criteria, baseVersion: rubric.version },
      {
        onSuccess: (saved) => {
          toast.success(`Saved version ${String(saved.version)}`);
        },
        onError: (error) => {
          toast.error(error.message);
        },
      },
    );
  }

  return (
    <Card>
      <CardHeader
        title="Rubric"
        hint="Drafted from the job description by the model, then edited and approved by you. Approving is the human gate: it is recorded against your name, and screening cannot start without it."
        aside={
          <span className="text-sm text-neutral-500 dark:text-neutral-400">
            Version {rubric.version} · <code>{shortHash(rubric.rubric_hash)}</code>
          </span>
        }
      />
      <CardBody>
        {approved ? (
          <Alert tone="ok">
            <p>
              Approved by <strong>{rubric.approved_by}</strong>. Editing below creates a new,
              unapproved version — this one stays exactly as it was screened against.
            </p>
          </Alert>
        ) : null}

        <Table caption="Rubric criteria">
          <thead>
            <tr>
              <Th className="w-10">ID</Th>
              <Th>Criterion</Th>
              <Th className="w-24">Must have</Th>
              <Th className="w-20">Weight</Th>
              <Th className="w-10">
                <span className="sr-only">Remove</span>
              </Th>
            </tr>
          </thead>
          <tbody>
            {criteria.map((criterion, index) => (
              <tr key={criterion.id}>
                <Td className="text-xs text-neutral-500">{criterion.id}</Td>
                <Td className="space-y-2">
                  <input
                    value={criterion.text}
                    aria-label={`Criterion ${criterion.id} text`}
                    onChange={(event) => {
                      update(index, { text: event.target.value });
                    }}
                  />
                </Td>
                <Td>
                  <input
                    type="checkbox"
                    checked={criterion.must_have}
                    aria-label={`${criterion.id} is a must-have`}
                    onChange={(event) => {
                      update(index, { must_have: event.target.checked });
                    }}
                  />
                </Td>
                <Td>
                  <select
                    value={criterion.weight}
                    aria-label={`${criterion.id} weight`}
                    onChange={(event) => {
                      update(index, { weight: Number(event.target.value) });
                    }}
                  >
                    {WEIGHTS.map((weight) => (
                      <option key={weight} value={weight}>
                        {weight}
                      </option>
                    ))}
                  </select>
                </Td>
                <Td>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`Remove ${criterion.id}`}
                    onClick={() => {
                      setCriteria((current) => current.filter((_, i) => i !== index));
                    }}
                  >
                    <Trash2 className="size-4" aria-hidden />
                  </Button>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>

        <p className="text-sm text-neutral-500 dark:text-neutral-400">
          A <strong>must-have</strong> is a hard requirement: failing one moves the candidate to the
          unqualified group, ranked separately and never against the rest. Mark them sparingly.
        </p>

        {problem ? (
          <Alert tone="error">
            <p>{problem}</p>
          </Alert>
        ) : null}

        <div className="flex flex-wrap items-center gap-2">
          <Button
            disabled={criteria.length >= MAX_CRITERIA}
            onClick={() => {
              setCriteria((current) => [
                ...current,
                {
                  id: nextCriterionId(current),
                  text: '',
                  claim: '',
                  claim_stale: false,
                  must_have: false,
                  weight: 1,
                },
              ]);
            }}
          >
            Add criterion
          </Button>
          <Button disabled={!dirty} busy={save.isPending} onClick={onSave}>
            Save as new version
          </Button>
          <Button
            variant="primary"
            disabled={approved || dirty}
            busy={approve.isPending}
            title={dirty ? 'Save your edits first — approval applies to a stored version' : ''}
            onClick={() => {
              approve.mutate(rubric.id, {
                onSuccess: (result) => {
                  toast.success(`Approved by ${result.approved_by ?? 'you'}`);
                },
                onError: (error) => {
                  toast.error(error.message);
                },
              });
            }}
          >
            Approve version {rubric.version}
          </Button>
          {dirty ? (
            <span className="text-sm text-neutral-500 dark:text-neutral-400">Unsaved edits</span>
          ) : null}
          <Button
            variant="ghost"
            busy={extract.isPending}
            onClick={() => {
              extract.mutate(undefined, {
                onSuccess: (drafted) => {
                  toast.success(`Drafted version ${String(drafted.version)}`);
                },
                onError: (error) => {
                  toast.error(error.message);
                },
              });
            }}
          >
            Redraft from the job description
          </Button>
        </div>
        {extract.isPending ? (
          <p className="text-sm text-neutral-500 dark:text-neutral-400">
            This is a live model call and can take a minute on a cold start.
          </p>
        ) : (
          <p className="text-sm text-neutral-500 dark:text-neutral-400">
            Redrafting creates a new version from the job description, discarding any unsaved edits
            above — the version you last saved or approved is never lost.
          </p>
        )}
      </CardBody>
    </Card>
  );
}
