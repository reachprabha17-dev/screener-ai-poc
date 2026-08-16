import * as RadixPopover from '@radix-ui/react-popover';
import type { ReactNode } from 'react';
import { cn } from './cn';

/**
 * A popover, from Radix rather than from a `<details>` element and a click-outside
 * handler.
 *
 * The header controls need focus trapping, dismissal on Escape and outside click,
 * correct `aria-expanded` wiring and collision-aware positioning. Every one of
 * those is a known-difficult detail, and Radix is the library that already has
 * them right.
 */
export function Popover({
  trigger,
  className,
  children,
}: {
  trigger: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  return (
    <RadixPopover.Root>
      <RadixPopover.Trigger asChild>{trigger}</RadixPopover.Trigger>
      <RadixPopover.Portal>
        <RadixPopover.Content
          sideOffset={8}
          align="end"
          className={cn(
            'z-50 w-80 rounded-xl border border-neutral-200 bg-white p-4 shadow-lg',
            'dark:border-neutral-800 dark:bg-neutral-900',
            className,
          )}
        >
          {children}
          <RadixPopover.Arrow className="fill-neutral-200 dark:fill-neutral-800" />
        </RadixPopover.Content>
      </RadixPopover.Portal>
    </RadixPopover.Root>
  );
}
