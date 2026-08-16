import { Component, type ErrorInfo, type ReactNode } from 'react';
import { Button } from '../ui/Button';
import { Card, CardBody } from '../ui/Card';

/**
 * The last stop before a white screen.
 *
 * A stack trace mid-page is useless to a recruiter and indistinguishable from a
 * bug in the screening itself — the same reason the Streamlit deployment set
 * `client.showErrorDetails = "none"`. A render failure here means this app has a
 * bug, so it says that, and offers the one action that reliably helps.
 *
 * Still a class component: error boundaries are the one part of React with no
 * hook equivalent.
 */
interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('Reviewer interface failed to render', error, info.componentStack);
  }

  override render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <main className="mx-auto max-w-2xl p-6">
        <Card>
          <CardBody>
            <h1 className="text-xl font-semibold">This screen could not be displayed</h1>
            <p className="text-sm">
              The reviewer interface hit a problem rendering. Nothing was submitted, and no
              screening result was changed.
            </p>
            <p className="text-sm text-neutral-500 dark:text-neutral-400">{error.message}</p>
            <Button
              variant="primary"
              onClick={() => {
                this.setState({ error: null });
              }}
            >
              Try again
            </Button>
          </CardBody>
        </Card>
      </main>
    );
  }
}
