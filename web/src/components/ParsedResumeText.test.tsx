import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { ParsedResumeText } from './ParsedResumeText';

describe('parsed resume text', () => {
  it('is collapsed by default and reveals the parsed text on open', async () => {
    const user = userEvent.setup();
    render(<ParsedResumeText resumeText="Jane Doe\nSenior Engineer\n5 years Python" />);

    expect(screen.queryByText(/5 years Python/)).not.toBeVisible();

    await user.click(screen.getByText('Parsed resume text'));

    expect(screen.getByText(/5 years Python/)).toBeVisible();
  });

  it('preserves line breaks rather than reflowing into one paragraph', async () => {
    const user = userEvent.setup();
    render(<ParsedResumeText resumeText={'Line one\nLine two'} />);
    await user.click(screen.getByText('Parsed resume text'));

    const block = screen.getByText((_, element) => element?.tagName === 'PRE');
    expect(block.textContent).toBe('Line one\nLine two');
  });
});
