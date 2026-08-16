import type { ReactNode, ThHTMLAttributes, TdHTMLAttributes } from 'react';
import { cn } from './cn';

/**
 * Plain semantic tables, styled once.
 *
 * No data-grid library: these tables are read top to bottom and the ordering that
 * matters — needs-review first, then rank within a group — is the server's, not
 * something a reviewer should be able to re-sort. Sorting a ranked list by
 * filename is how the top of Band A stops being the top of the screen.
 */
export function Table({ caption, children }: { caption: string; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">{caption}</caption>
        {children}
      </table>
    </div>
  );
}

export function Th({ className, children, ...props }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      className={cn(
        'border-b border-neutral-200 px-3 py-2 text-left text-xs font-semibold tracking-wide',
        'text-neutral-500 uppercase dark:border-neutral-800 dark:text-neutral-400',
        className,
      )}
      {...props}
    >
      {children}
    </th>
  );
}

export function Td({ className, children, ...props }: TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td
      className={cn(
        'border-b border-neutral-100 px-3 py-2 align-top dark:border-neutral-800/70',
        className,
      )}
      {...props}
    >
      {children}
    </td>
  );
}
