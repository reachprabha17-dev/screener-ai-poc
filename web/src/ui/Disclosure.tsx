import { ChevronRight } from 'lucide-react';
import type { ReactNode } from 'react';
import { cn } from './cn';

/**
 * A native `<details>`, styled.
 *
 * Not a JavaScript accordion: the browser already implements the open/closed
 * state, the keyboard behaviour and the accessibility semantics, and — the part
 * that matters on a compliance screen — the content inside is findable by the
 * browser's own in-page search even while collapsed.
 */
export function Disclosure({
  summary,
  defaultOpen = false,
  className,
  children,
}: {
  summary: ReactNode;
  defaultOpen?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <details
      open={defaultOpen}
      className={cn(
        'group rounded-lg border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900',
        className,
      )}
    >
      <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-2.5 text-sm font-medium marker:hidden">
        <ChevronRight
          className="size-4 shrink-0 text-neutral-400 transition-transform group-open:rotate-90"
          aria-hidden
        />
        {summary}
      </summary>
      <div className="space-y-3 px-4 pb-4">{children}</div>
    </details>
  );
}
