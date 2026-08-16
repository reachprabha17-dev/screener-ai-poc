import type { ReactNode } from 'react';
import { cn } from './cn';

/** The one container in the app. Everything sits in one of these. */
export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <section
      className={cn(
        'rounded-xl border border-neutral-200 bg-white shadow-sm dark:border-neutral-800 dark:bg-neutral-900',
        className,
      )}
    >
      {children}
    </section>
  );
}

export function CardBody({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cn('space-y-4 p-5', className)}>{children}</div>;
}

export function CardHeader({
  title,
  aside,
  hint,
}: {
  title: ReactNode;
  aside?: ReactNode;
  hint?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-neutral-200 p-5 dark:border-neutral-800">
      <div className="min-w-0">
        <h2 className="text-lg font-semibold">{title}</h2>
        {hint ? (
          <p className="mt-1 text-sm text-neutral-500 dark:text-neutral-400">{hint}</p>
        ) : null}
      </div>
      {aside}
    </div>
  );
}
