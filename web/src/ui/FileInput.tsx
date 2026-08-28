import { Upload } from 'lucide-react';
import { useId, useRef, useState, type ChangeEvent, type DragEvent } from 'react';
import { cn } from './cn';

/**
 * Choose one file, by clicking or by dropping.
 *
 * **The native control is visually hidden rather than styled.** `index.css`
 * styles every `input` that is not a checkbox or radio, which catches
 * `type="file"` and gives it a text-field border around a browser-drawn button
 * — a control that looks like an input and behaves like nothing else on the
 * page. It stays in the DOM, focusable and labelled, so keyboard and screen
 * reader users get the real thing; only its appearance is replaced.
 *
 * **`accept` is a convenience, never a check.** It filters the file picker's
 * default view and is trivially bypassed by choosing "all files" — which is why
 * the server sniffs magic bytes and does not consult the extension at all.
 * Nothing here is a security control.
 */
export function FileInput({
  accept,
  onSelect,
  disabled = false,
  hint,
}: {
  accept: string;
  onSelect: (file: File) => void;
  disabled?: boolean;
  hint?: string;
}) {
  const inputId = useId();
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  function take(file: File | undefined) {
    if (file) onSelect(file);
  }

  function onChange(event: ChangeEvent<HTMLInputElement>) {
    take(event.target.files?.[0]);
    // Cleared so choosing the same file twice fires again. Without this, a
    // reviewer who uploads a document, sees a bad reading, fixes the file and
    // picks it again gets nothing at all — the value did not change.
    event.target.value = '';
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    if (!disabled) take(event.dataTransfer.files[0]);
  }

  return (
    <div
      onDragOver={(event) => {
        event.preventDefault();
        if (!disabled) setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
      className={cn(
        'rounded-lg border border-dashed px-4 py-6 text-center transition-colors',
        dragging
          ? 'border-blue-500 bg-blue-50 dark:bg-blue-950/30'
          : 'border-neutral-300 dark:border-neutral-700',
        disabled && 'opacity-60',
      )}
    >
      <input
        ref={input}
        id={inputId}
        type="file"
        accept={accept}
        disabled={disabled}
        onChange={onChange}
        className="sr-only"
      />
      <Upload className="mx-auto size-5 text-neutral-400" aria-hidden />
      <p className="mt-2 text-sm">
        <label
          htmlFor={inputId}
          className="cursor-pointer font-medium text-blue-600 hover:underline dark:text-blue-400"
        >
          Choose a file
        </label>{' '}
        <span className="text-neutral-500 dark:text-neutral-400">or drop one here</span>
      </p>
      {hint ? <p className="mt-1 text-xs text-neutral-500 dark:text-neutral-400">{hint}</p> : null}
    </div>
  );
}
