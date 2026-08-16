import type { ReactNode } from 'react';
import { cn } from './cn';

/**
 * A labelled control.
 *
 * A `<label>` wrapping its input rather than an `id`/`htmlFor` pair: there is no
 * id to collide when the same form renders twice on a screen, which the review
 * page does for every candidate group.
 */
export function Field({
  label,
  hint,
  className,
  children,
}: {
  label: ReactNode;
  hint?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  return (
    <label className={cn('block space-y-1.5', className)}>
      <span className="block text-sm font-medium">{label}</span>
      {children}
      {hint ? (
        <span className="block text-xs text-neutral-500 dark:text-neutral-400">{hint}</span>
      ) : null}
    </label>
  );
}

/** A radio or checkbox with its text, aligned. */
export function Choice({
  children,
  className,
  ...props
}: React.InputHTMLAttributes<HTMLInputElement> & { children: ReactNode }) {
  return (
    <label className={cn('inline-flex items-center gap-2 text-sm', className)}>
      <input {...props} />
      {children}
    </label>
  );
}
