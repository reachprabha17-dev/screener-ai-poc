import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { CriterionView } from '../api/types';
import { CriterionBlock } from './CriterionBlock';

const RESUME_TEXT = 'Experienced Python engineer. Built production services at scale.';

function criterion(overrides: Partial<CriterionView>): CriterionView {
  return {
    id: 'C1',
    text: 'Has production experience with Python.',
    weight: 3,
    must_have: false,
    verdict: 'partial',
    evidence: '',
    evidence_status: 'not_applicable',
    highlights: [],
    negation_suspected: false,
    verifier: null,
    ...overrides,
  };
}

describe('criterion block evidence rendering', () => {
  it('renders a genuinely verified quote as a blockquote, highlighted', () => {
    render(
      <CriterionBlock
        criterion={criterion({
          evidence: 'Python engineer',
          evidence_status: 'verified',
          highlights: [{ start: 12, end: 27 }],
        })}
        resumeText={RESUME_TEXT}
      />,
    );

    const quote = document.querySelector('blockquote');
    expect(quote).not.toBeNull();
    expect(screen.getByText('Python engineer')).toBeInTheDocument();
    expect(screen.queryByText(/not found in the resume/i)).not.toBeInTheDocument();
  });

  it('does not render a fabricated quote as a blockquote, and warns instead', () => {
    render(
      <CriterionBlock
        criterion={criterion({
          text: 'Holds a valid UAE driving license with good driving skill and experience.',
          evidence: 'Holds a valid UAE driving license with good driving skill and experience.',
          evidence_status: 'unverified',
          highlights: [],
        })}
        resumeText={RESUME_TEXT}
      />,
    );

    expect(document.querySelector('blockquote')).toBeNull();
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent(/not found in the resume/i);
    expect(alert).toHaveTextContent(
      /Holds a valid UAE driving license with good driving skill and experience\./,
    );
  });

  it('renders an honest "not found" answer plainly, not as a warning', () => {
    render(
      <CriterionBlock
        criterion={criterion({
          verdict: 'none',
          evidence: 'not found',
          evidence_status: 'not_applicable',
          highlights: [],
        })}
        resumeText={RESUME_TEXT}
      />,
    );

    expect(document.querySelector('blockquote')).not.toBeNull();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
