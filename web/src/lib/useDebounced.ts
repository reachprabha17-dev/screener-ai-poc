import { useEffect, useState } from 'react';

/**
 * A value that settles before anything acts on it.
 *
 * Used by the folder search. Filtering happens server-side because the cost being
 * avoided is server-side — counting a folder's resumes is a recursive walk over a
 * network mount — so a request per keystroke is the expensive mistake here, not a
 * slightly late list.
 */
export function useDebounced<T>(value: T, delayMs = 250): T {
  const [settled, setSettled] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => {
      setSettled(value);
    }, delayMs);
    return () => {
      clearTimeout(timer);
    };
  }, [value, delayMs]);

  return settled;
}
