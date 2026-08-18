import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ThemeToggle } from './ThemeToggle';

function mockMatchMedia(prefersDark: boolean): void {
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockImplementation((query: string) => ({
      matches: query === '(prefers-color-scheme: dark)' && prefersDark,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.classList.remove('dark');
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('theme toggle', () => {
  it('defaults to the OS preference and cycles light → dark → system', async () => {
    mockMatchMedia(false);
    const user = userEvent.setup();
    render(<ThemeToggle />);

    // System, OS is light: not dark yet.
    expect(document.documentElement.classList.contains('dark')).toBe(false);

    const button = screen.getByRole('button');
    await user.click(button); // -> light
    expect(document.documentElement.classList.contains('dark')).toBe(false);
    expect(localStorage.getItem('screener-theme')).toBe('light');

    await user.click(button); // -> dark
    expect(document.documentElement.classList.contains('dark')).toBe(true);
    expect(localStorage.getItem('screener-theme')).toBe('dark');

    await user.click(button); // -> system, OS is light
    expect(document.documentElement.classList.contains('dark')).toBe(false);
    expect(localStorage.getItem('screener-theme')).toBe('system');
  });

  it('an explicit dark choice overrides a light OS setting', () => {
    mockMatchMedia(false);
    localStorage.setItem('screener-theme', 'dark');

    render(<ThemeToggle />);

    expect(document.documentElement.classList.contains('dark')).toBe(true);
  });

  it('remembers the choice across a remount', () => {
    mockMatchMedia(false);
    localStorage.setItem('screener-theme', 'dark');

    const { unmount } = render(<ThemeToggle />);
    unmount();
    render(<ThemeToggle />);

    expect(screen.getByRole('button')).toHaveAccessibleName(/dark/i);
  });
});
