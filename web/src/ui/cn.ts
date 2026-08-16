import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/**
 * Merge class names, letting a caller's utility win over the component's default.
 *
 * `clsx` handles the conditionals, `tailwind-merge` resolves the conflicts —
 * without it, `<Button className="px-2">` produces `px-4 px-2` and which one
 * applies depends on the order Tailwind happened to emit them in.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
