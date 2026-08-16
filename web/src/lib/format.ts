/**
 * Formatting, from libraries rather than from string slicing.
 *
 * `date-fns` parses what the API sends (ISO 8601 out of pydantic, sometimes with
 * a zone and sometimes without) and formats it in the reviewer's locale offset;
 * hand-slicing the string showed everyone UTC and called it local time. `papaparse`
 * writes the CSV, because quoting is the part of CSV that is easy to get wrong and
 * a resume filename with a comma in it is not exotic.
 */

import { format, formatDistanceStrict, isValid, parseISO } from 'date-fns';
import Papa from 'papaparse';

function parse(value: string | null | undefined): Date | null {
  if (!value) return null;
  const parsed = parseISO(value);
  return isValid(parsed) ? parsed : null;
}

/** `2026-05-04 11:22` — enough to order events, not so much it becomes noise. */
export function dateTime(value: string | null | undefined): string {
  const parsed = parse(value);
  return parsed ? format(parsed, 'yyyy-MM-dd HH:mm') : '';
}

/** Just the date, for tables where the time adds noise rather than information. */
export function date(value: string | null | undefined): string {
  const parsed = parse(value);
  return parsed ? format(parsed, 'yyyy-MM-dd') : '';
}

/**
 * A count as a stat tile shows it: grouped up to five figures, compact beyond.
 *
 * `1,284` stays exact because a reviewer plans around it; `12.9K` is the point
 * at which the exact figure stops being the thing anyone reads, and eight digits
 * across a tile stop being one glance.
 */
export function compactCount(value: number): string {
  return value < 100_000
    ? new Intl.NumberFormat(undefined).format(value)
    : new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(
        value,
      );
}

export function percent(value: number, digits = 0): string {
  return `${(value * 100).toFixed(digits)}%`;
}

/** Seconds as something a person reads at a glance rather than counts. */
export function duration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '—';
  const now = Date.now();
  return formatDistanceStrict(now + seconds * 1000, now);
}

/** A short, still-unambiguous hash for display. */
export function shortHash(value: string | null | undefined, n = 12): string {
  return value ? value.slice(0, n) : '—';
}

/** Rows to CSV text, quoted properly. */
export function toCsv(rows: Record<string, unknown>[]): string {
  return Papa.unparse(rows, { newline: '\n' });
}

/**
 * Hand the browser a file without a round trip to the server.
 *
 * The rows are already in this tab — asking the API to render a CSV it does not
 * otherwise produce would add an egress path for candidate data that does not
 * need to exist.
 */
export function downloadCsv(filename: string, csv: string): void {
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
