import type { LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';
import { compactCount } from '../lib/format';
import { Card } from '../ui/Card';
import { cn } from '../ui/cn';

/**
 * One headline count.
 *
 * A stat tile rather than a one-bar chart: three current values have no shape to
 * plot, and a chart of them would be decoration that a reader has to decode
 * before arriving back at the number.
 *
 * **No delta and no sparkline**, because the system stores no history to compute
 * one from. A trend line drawn from the only figure available would be a picture
 * of nothing, and a reader cannot tell that by looking at it.
 *
 * **The value carries no colour.** Ink stays ink; identity comes from the icon
 * beside it. The one exception is `attention`, which is a *state* — work is
 * outstanding — and it arrives with an icon and a sentence, never as colour
 * alone.
 */
export function StatTile({
  label,
  value,
  icon: Icon,
  hint,
  attention = false,
}: {
  label: string;
  value: number;
  icon: LucideIcon;
  hint?: ReactNode;
  attention?: boolean;
}) {
  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm text-neutral-500 dark:text-neutral-400">{label}</p>
        <Icon
          className={cn(
            'size-5 shrink-0',
            attention ? 'text-amber-600 dark:text-amber-400' : 'text-neutral-400',
          )}
          aria-hidden
        />
      </div>
      {/* Proportional figures, not tabular: at this size `tabular-nums` gives
          every digit the width of a zero and a number like 121 reads loose. */}
      <p className="mt-1 text-4xl font-semibold tracking-tight">{compactCount(value)}</p>
      {hint ? <p className="mt-1 text-sm text-neutral-500 dark:text-neutral-400">{hint}</p> : null}
    </Card>
  );
}
