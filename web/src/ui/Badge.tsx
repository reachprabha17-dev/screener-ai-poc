import { cva, type VariantProps } from 'class-variance-authority';
import type { ReactNode } from 'react';
import { cn } from './cn';

const badge = cva(
  'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold whitespace-nowrap',
  {
    variants: {
      tone: {
        neutral: 'bg-neutral-200 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300',
        info: 'bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-200',
        ok: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200',
        warn: 'bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200',
        error: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200',
      },
    },
    defaultVariants: { tone: 'neutral' },
  },
);

export function Badge({
  tone,
  className,
  title,
  children,
}: VariantProps<typeof badge> & { className?: string; title?: string; children: ReactNode }) {
  return (
    <span className={cn(badge({ tone }), className)} title={title}>
      {children}
    </span>
  );
}
