/**
 * Light, dark, or the OS setting — with the choice remembered across visits.
 *
 * `dark:` utilities key off a `.dark` class on `<html>` (see the `@custom-variant`
 * in `index.css`), not `prefers-color-scheme` directly, so a picked theme can
 * override the OS instead of only ever following it. `system` re-derives the
 * class from the OS preference every time it is applied, which is what lets a
 * reviewer who has never touched the toggle still get the OS behaviour for free.
 */

export type Theme = 'light' | 'dark' | 'system';

const STORAGE_KEY = 'screener-theme';

function systemPrefersDark(): boolean {
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

function isDark(theme: Theme): boolean {
  return theme === 'dark' || (theme === 'system' && systemPrefersDark());
}

/** The remembered choice, or `system` for a reviewer who has never set one. */
export function getStoredTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY);
  return stored === 'light' || stored === 'dark' || stored === 'system' ? stored : 'system';
}

/**
 * Sets the class `dark:` utilities read, sets native `color-scheme` to match
 * (so browser-drawn controls — scrollbars, checkboxes — agree with the page
 * rather than following the OS on their own), and persists the choice.
 */
export function applyTheme(theme: Theme): void {
  localStorage.setItem(STORAGE_KEY, theme);
  const dark = isDark(theme);
  document.documentElement.classList.toggle('dark', dark);
  document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
}
