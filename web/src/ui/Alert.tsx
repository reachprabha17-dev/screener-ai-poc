import { cva, type VariantProps } from 'class-variance-authority';
import { AlertTriangle, CheckCircle2, Info, XCircle } from 'lucide-react';
import type { ReactNode } from 'react';
import { cn } from './cn';

const alert = cva('flex gap-3 rounded-lg border p-3 text-sm', {
  variants: {
    tone: {
      info: 'border-blue-200 bg-blue-50 text-blue-900 dark:border-blue-900 dark:bg-blue-950/50 dark:text-blue-100',
      ok: 'border-emerald-200 bg-emerald-50 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950/50 dark:text-emerald-100',
      warn: 'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/50 dark:text-amber-100',
      error:
        'border-red-200 bg-red-50 text-red-900 dark:border-red-900 dark:bg-red-950/50 dark:text-red-100',
    },
  },
  defaultVariants: { tone: 'info' },
});

const ICONS = {
  info: Info,
  ok: CheckCircle2,
  warn: AlertTriangle,
  error: XCircle,
};

export type Tone = keyof typeof ICONS;

/**
 * Anything the reviewer is told, in one shape.
 *
 * `role="alert"` on the two urgent tones only: a screen reader announcing every
 * informational caption on this page would bury the one that says a candidate was
 * not scored.
 */
export function Alert({
  tone = 'info',
  title,
  className,
  children,
}: VariantProps<typeof alert> & {
  tone?: Tone;
  title?: string;
  className?: string;
  children?: ReactNode;
}) {
  const Icon = ICONS[tone];
  const urgent = tone === 'error' || tone === 'warn';

  return (
    <div className={cn(alert({ tone }), className)} {...(urgent ? { role: 'alert' } : {})}>
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div className="min-w-0 space-y-1">
        {title ? <p className="font-semibold">{title}</p> : null}
        {children}
      </div>
    </div>
  );
}
