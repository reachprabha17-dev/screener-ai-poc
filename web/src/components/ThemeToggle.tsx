import { Monitor, Moon, Sun } from 'lucide-react';
import { useEffect, useState } from 'react';
import { applyTheme, getStoredTheme, type Theme } from '../lib/theme';

const NEXT: Record<Theme, Theme> = { light: 'dark', dark: 'system', system: 'light' };
const ICON = { light: Sun, dark: Moon, system: Monitor } as const;
const LABEL = { light: 'Light', dark: 'Dark', system: 'System' } as const;

/**
 * One button, cycling light → dark → system.
 *
 * Not a three-way switch: this is reached once in a while, not tuned daily, and
 * a single control that always shows the *current* state needs no extra chrome
 * to explain which of three buttons is active.
 */
export function ThemeToggle() {
  const [theme, setThemeState] = useState<Theme>(getStoredTheme);

  useEffect(() => {
    applyTheme(theme);
    if (theme !== 'system') return undefined;

    // Stored choice is "system" — keep the page in step if the OS setting
    // changes while it is open, not only at the next reload.
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = (): void => {
      applyTheme('system');
    };
    media.addEventListener('change', onChange);
    return () => {
      media.removeEventListener('change', onChange);
    };
  }, [theme]);

  const Icon = ICON[theme];

  return (
    <button
      type="button"
      title={`Theme: ${LABEL[theme]}. Click to switch.`}
      aria-label={`Theme: ${LABEL[theme]}. Click to switch.`}
      onClick={() => {
        setThemeState(NEXT[theme]);
      }}
      className="flex items-center rounded-lg p-1.5 text-neutral-600 hover:bg-neutral-100 dark:text-neutral-300 dark:hover:bg-neutral-800"
    >
      <Icon className="size-4" aria-hidden />
    </button>
  );
}
