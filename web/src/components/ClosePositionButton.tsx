import { Archive } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { useClosePosition } from '../api/queries';
import type { Position } from '../api/types';
import { Alert } from '../ui/Alert';
import { Button } from '../ui/Button';
import { Popover } from '../ui/Popover';

/**
 * Close a requisition: the post is filled, or it is not being filled.
 *
 * **Confirmed, because it is not visibly reversible from here.** Closing removes
 * the requisition from every list in the app, and nothing in the interface
 * reopens it — so the confirmation says what will happen and what will not, in
 * the two sentences someone actually reads before clicking.
 *
 * **The reassurance is the important half.** "Close" next to a list of
 * candidates reads like a delete, and a reviewer who believes it might destroy
 * an adverse-action record will simply never press it — leaving filled posts
 * cluttering the queue forever. Nothing is deleted: the runs, the candidates and
 * the audit trail are untouched and stay reachable under Runs.
 */
export function ClosePositionButton({ position }: { position: Position }) {
  const [open, setOpen] = useState(false);
  const close = useClosePosition();
  const navigate = useNavigate();

  if (position.status === 'closed') return null;

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      trigger={
        <Button size="sm">
          <Archive className="size-3.5" aria-hidden />
          Close requisition
        </Button>
      }
    >
      <div className="space-y-3">
        <p className="text-sm font-semibold">Close {position.reference}?</p>
        <p className="text-sm text-neutral-600 dark:text-neutral-300">
          It leaves the requisitions list and stops counting as an active job posting.
        </p>
        <p className="text-sm text-neutral-600 dark:text-neutral-300">
          Runs already screening for it <strong>carry on</strong>, and can be reviewed and signed
          off as normal. To stop one, abort it on the run itself.
        </p>
        <p className="text-sm text-neutral-600 dark:text-neutral-300">
          Nothing is deleted: its runs, candidates and decisions stay readable under{' '}
          <strong>Runs</strong>.
        </p>

        {close.isError ? (
          <Alert tone="error">
            <p>{close.error.message}</p>
          </Alert>
        ) : null}

        <div className="flex gap-2">
          <Button
            variant="primary"
            size="sm"
            busy={close.isPending}
            onClick={() => {
              close.mutate(position.id, {
                onSuccess: (closed) => {
                  toast.success(`${closed.reference} closed`);
                  setOpen(false);
                  void navigate('/requisitions');
                },
                // Left open on failure so the reason stays on screen next to
                // the button that produced it.
              });
            }}
          >
            Close it
          </Button>
          <Button
            size="sm"
            onClick={() => {
              setOpen(false);
            }}
          >
            Cancel
          </Button>
        </div>
      </div>
    </Popover>
  );
}
