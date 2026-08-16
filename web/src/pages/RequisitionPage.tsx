import { Link, useNavigate, useParams } from 'react-router-dom';
import { toast } from 'sonner';
import {
  useApprovedRubric,
  useCreateRun,
  useExtractRubric,
  useLatestRubric,
  usePositions,
  useRuns,
} from '../api/queries';
import { QueryState } from '../components/QueryState';
import { RubricEditor } from '../components/RubricEditor';
import { RunList } from '../components/RunList';
import { Alert } from '../ui/Alert';
import { Button } from '../ui/Button';
import { Card, CardBody, CardHeader } from '../ui/Card';

/**
 * One requisition: its rubric, and the runs screened against it.
 *
 * The whole flow lives on one screen in the order it happens — draft a rubric,
 * edit it, approve it, start a run — because it is a sequence with a gate in the
 * middle, and a reviewer who has to navigate between steps loses track of which
 * one they are waiting on.
 */
export function RequisitionPage() {
  const { positionId = '' } = useParams();
  const positions = usePositions();
  const latest = useLatestRubric(positionId);
  const approved = useApprovedRubric(positionId);
  const runs = useRuns();
  const extract = useExtractRubric(positionId);
  const createRun = useCreateRun();
  const navigate = useNavigate();

  const position = positions.data?.find((p) => p.id === positionId);

  return (
    <div className="space-y-4">
      <p className="text-sm text-neutral-500 dark:text-neutral-400">
        <Link to="/requisitions" className="hover:underline">
          Requisitions
        </Link>{' '}
        / {position?.reference ?? positionId}
      </p>

      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          {position?.title ?? 'Requisition'}
        </h1>
        <p className="text-sm text-neutral-500 dark:text-neutral-400">
          <code>{position?.reference ?? positionId}</code>
          {position ? ` · raised by ${position.created_by}` : null}
        </p>
      </div>

      <QueryState query={latest} loading="Loading the rubric…">
        {(rubric) =>
          rubric === null ? (
            <Card>
              <CardHeader
                title="Rubric"
                hint="The model reads the job description and proposes criteria; you edit them and approve. Nothing is screened until you do."
              />
              <CardBody>
                <Button
                  variant="primary"
                  busy={extract.isPending}
                  onClick={() => {
                    extract.mutate(undefined, {
                      onError: (error) => {
                        toast.error(error.message);
                      },
                    });
                  }}
                >
                  Draft from the job description
                </Button>
                {extract.isPending ? (
                  <p className="text-sm text-neutral-500 dark:text-neutral-400">
                    This is a live model call and can take a minute on a cold start.
                  </p>
                ) : null}
              </CardBody>
            </Card>
          ) : (
            // Keyed on the rubric id: a saved version is a different rubric, and
            // the editor's contents should be replaced rather than merged.
            <RubricEditor key={rubric.id} rubric={rubric} />
          )
        }
      </QueryState>

      <Card>
        <CardHeader title="Screening runs" />
        <CardBody>
          <QueryState query={approved} loading="Checking for an approved rubric…">
            {(approvedRubric) =>
              approvedRubric === null ? (
                <Alert tone="info">
                  <p>
                    No approved rubric for this requisition yet. Approve one above before starting a
                    run.
                  </p>
                </Alert>
              ) : (
                <div className="space-y-3">
                  <p className="text-sm text-neutral-500 dark:text-neutral-400">
                    A run snapshots the folder into a fixed set of files and screens each one
                    against approved rubric v{approvedRubric.version} (
                    {approvedRubric.criteria.length} criteria, approved by{' '}
                    {approvedRubric.approved_by}). Files added later are picked up only by an
                    explicit rescan.
                  </p>
                  <Button
                    variant="primary"
                    busy={createRun.isPending}
                    onClick={() => {
                      createRun.mutate(
                        { position_id: positionId, rubric_id: approvedRubric.id },
                        {
                          onSuccess: (run) => {
                            toast.success(`Snapshotted ${String(run.file_count)} file(s)`);
                            void navigate(`/runs/${run.id}`);
                          },
                          onError: (error) => {
                            toast.error(error.message);
                          },
                        },
                      );
                    }}
                  >
                    New screening run
                  </Button>
                </div>
              )
            }
          </QueryState>

          <QueryState query={runs}>
            {(all) => <RunList runs={all.filter((run) => run.position_id === positionId)} />}
          </QueryState>
        </CardBody>
      </Card>
    </div>
  );
}
