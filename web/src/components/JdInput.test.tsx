import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AppConfig, JdDocument } from '../api/types';
import { renderWithProviders } from '../test/utils';
import { emptyJd, type JdValue } from '../lib/jd';
import { JdInput } from './JdInput';

const CONFIG: AppConfig = {
  jd_intake_mode: 'both',
  jd_max_file_bytes: 2 * 1024 * 1024,
  jd_max_pages: 10,
  allowed_extensions: ['.pdf', '.docx'],
};

const DOCUMENT: JdDocument = {
  text: 'Senior Backend Engineer. Kubernetes is essential.',
  filename: 'senior-backend.pdf',
  file_sha256: 'a'.repeat(64),
  page_count: 2,
  ocr_used: false,
  chars_stripped: 0,
  warnings: [],
  injection_signals: [],
  parser_version: 'xberg/1.0',
};

/** `fetch` accepts three argument shapes; only one of them stringifies usefully. */
function pathOf(input: RequestInfo | URL): string {
  if (typeof input === 'string') return input;
  if (input instanceof URL) return input.pathname;
  return input.url;
}

/**
 * Route by path, because this component makes two different calls and the whole
 * question in most of these tests is what came back from which.
 */
function mockApi({
  config = CONFIG,
  document,
  uploadStatus = 200,
  uploadBody,
}: {
  config?: Partial<AppConfig>;
  document?: Partial<JdDocument>;
  uploadStatus?: number;
  uploadBody?: unknown;
} = {}) {
  const mock = vi.fn<typeof fetch>((input) => {
    const url = pathOf(input);
    if (url.includes('/config')) {
      return Promise.resolve(
        new Response(JSON.stringify({ ...CONFIG, ...config }), { status: 200 }),
      );
    }
    return Promise.resolve(
      new Response(JSON.stringify(uploadBody ?? { ...DOCUMENT, ...document }), {
        status: uploadStatus,
      }),
    );
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

/** The component is controlled, so a test needs somewhere to keep the value. */
function Harness() {
  const [value, setValue] = useState<JdValue>(emptyJd);
  return (
    <>
      <JdInput value={value} onChange={setValue} />
      <output data-testid="source">{value.source}</output>
      <output data-testid="filename">{value.filename ?? ''}</output>
    </>
  );
}

function pdf(name = 'senior-backend.pdf') {
  return new File([new Uint8Array([0x25, 0x50, 0x44, 0x46])], name, { type: 'application/pdf' });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('which controls are offered', () => {
  it('offers both when the deployment allows both', async () => {
    mockApi();

    renderWithProviders(<Harness />);

    expect(await screen.findByText(/drop one here/i)).toBeInTheDocument();
    expect(screen.getByRole('textbox')).toBeInTheDocument();
  });

  it('hides the paste box when the deployment is upload-only', async () => {
    mockApi({ config: { jd_intake_mode: 'upload' } });

    renderWithProviders(<Harness />);

    // Waited for rather than asserted immediately: until `/config` answers, the
    // component renders the permissive default, and asserting before then would
    // pass against a component that never read the setting at all.
    await waitFor(() => {
      expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    });
    expect(screen.getByText(/drop one here/i)).toBeInTheDocument();
  });

  it('hides the file control when the deployment is paste-only', async () => {
    mockApi({ config: { jd_intake_mode: 'paste' } });

    renderWithProviders(<Harness />);

    await waitFor(() => {
      expect(screen.queryByText(/drop one here/i)).not.toBeInTheDocument();
    });
    expect(screen.getByRole('textbox')).toBeInTheDocument();
  });
});

describe('reading a document', () => {
  it('puts the extracted text in a box the reviewer can still edit', async () => {
    // The whole reason upload is two steps. A two-column PDF that interleaves
    // produces a plausible rubric, and the approval gate cannot catch it because
    // there is nothing to compare against — so the text stays editable.
    mockApi();
    const user = userEvent.setup();

    renderWithProviders(<Harness />);
    await user.upload(await screen.findByLabelText(/choose a file/i), pdf());

    const textarea = await screen.findByRole<HTMLTextAreaElement>('textbox');
    await waitFor(() => {
      expect(textarea.value).toContain('Kubernetes is essential');
    });

    await user.type(textarea, ' And Go.');
    expect(textarea.value).toContain('And Go.');
    expect(textarea).not.toHaveAttribute('readonly');
  });

  it('carries the provenance so the requisition records its source', async () => {
    mockApi();
    const user = userEvent.setup();

    renderWithProviders(<Harness />);
    await user.upload(await screen.findByLabelText(/choose a file/i), pdf());

    expect(await screen.findByTestId('source')).toHaveTextContent('upload');
    expect(screen.getByTestId('filename')).toHaveTextContent('senior-backend.pdf');
  });

  it('sends the file as multipart without a hand-written content type', async () => {
    // Setting it ourselves drops the boundary the browser generates, and the
    // server then cannot split the parts — a 422 for a request that was fine.
    const mock = mockApi();
    const user = userEvent.setup();

    renderWithProviders(<Harness />);
    await user.upload(await screen.findByLabelText(/choose a file/i), pdf());

    await waitFor(() => {
      expect(mock.mock.calls.some(([url]) => pathOf(url) === '/jd-documents')).toBe(true);
    });
    const call = mock.mock.calls.find(([url]) => pathOf(url) === '/jd-documents');
    const init = call?.[1];
    expect(init?.body).toBeInstanceOf(FormData);
    expect(init?.headers).not.toHaveProperty('Content-Type');
  });

  it('warns when the text was recognised rather than read', async () => {
    // Not decoration: a rubric drafted from OCR is drafted from an approximation.
    mockApi({ document: { ocr_used: true } });
    const user = userEvent.setup();

    renderWithProviders(<Harness />);
    await user.upload(await screen.findByLabelText(/choose a file/i), pdf());

    expect(await screen.findByText(/read by OCR/i)).toBeInTheDocument();
  });

  it('flags instruction-like text without refusing the document', async () => {
    // Advisory only. The patterns were calibrated against resume prose and a job
    // description says several of those things routinely, so this points at a
    // paragraph rather than making a claim about it.
    mockApi({ document: { injection_signals: ['role_hijack'] } });
    const user = userEvent.setup();

    renderWithProviders(<Harness />);
    await user.upload(await screen.findByLabelText(/choose a file/i), pdf());

    expect(await screen.findByText(/instruction-like text/i)).toBeInTheDocument();
    const textarea = screen.getByRole<HTMLTextAreaElement>('textbox');
    expect(textarea.value).toContain('Kubernetes is essential');
  });
});

describe('when a document cannot be read', () => {
  it("shows the server's sentence and leaves the paste box available", async () => {
    mockApi({
      uploadStatus: 400,
      uploadBody: { detail: 'That document could not be read. It may be corrupt.' },
    });
    const user = userEvent.setup();

    renderWithProviders(<Harness />);
    await user.upload(await screen.findByLabelText(/choose a file/i), pdf());

    expect(await screen.findByText(/may be corrupt/i)).toBeInTheDocument();
    expect(screen.getByText(/paste the text below instead/i)).toBeInTheDocument();
    expect(screen.getByRole('textbox')).toBeInTheDocument();
  });

  it('does not blame the document when the failure was not the document', async () => {
    // A 404 is what an API that has not been restarted answers, and it is the
    // failure most likely to be met during a deploy. Reporting it as "that
    // document could not be read" sends somebody off to re-export a file that
    // was never the problem.
    mockApi({ uploadStatus: 404, uploadBody: {} });
    const user = userEvent.setup();

    renderWithProviders(<Harness />);
    await user.upload(await screen.findByLabelText(/choose a file/i), pdf());

    expect(await screen.findByText(/upload did not work/i)).toBeInTheDocument();
    expect(screen.queryByText(/could not be read/i)).not.toBeInTheDocument();
  });

  it('explains a file that was too large', async () => {
    mockApi({ uploadStatus: 413, uploadBody: {} });
    const user = userEvent.setup();

    renderWithProviders(<Harness />);
    await user.upload(await screen.findByLabelText(/choose a file/i), pdf());

    expect(await screen.findByText(/too large/i)).toBeInTheDocument();
  });

  it('lets the reviewer remove a document and start again', async () => {
    mockApi();
    const user = userEvent.setup();

    renderWithProviders(<Harness />);
    await user.upload(await screen.findByLabelText(/choose a file/i), pdf());
    const remove = await screen.findByRole('button', { name: /remove this document/i });

    await user.click(remove);

    expect(screen.queryByRole('button', { name: /remove this document/i })).not.toBeInTheDocument();
    expect(screen.getByTestId('source')).toHaveTextContent('paste');
    expect(screen.getByRole<HTMLTextAreaElement>('textbox').value).toBe('');
    expect(screen.getByText(/drop one here/i)).toBeInTheDocument();
  });
});
