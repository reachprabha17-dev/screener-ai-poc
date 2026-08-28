import { FileText, X } from 'lucide-react';
import { useState } from 'react';
import { useConfig, useExtractJdDocument } from '../api/queries';
import type { JdDocument } from '../api/types';
import { emptyJd, type JdValue } from '../lib/jd';
import { Alert } from '../ui/Alert';
import { Field } from '../ui/Field';
import { FileInput } from '../ui/FileInput';

/**
 * The job description: uploaded and read, or pasted.
 *
 * **Upload is a two-step flow on purpose, and the second step is the point.**
 * The document is read, the text comes back into an editable box, and only then
 * is the requisition created. A two-column PDF that interleaves, or a scan whose
 * OCR dropped a "not", produces a perfectly plausible rubric — and the human
 * approval gate in front of that rubric cannot catch it, because the reviewer
 * has nothing to compare it against. Showing what the machine actually read is
 * that same control, applied one step earlier.
 *
 * So the textarea is never read-only after an upload. What the reviewer submits
 * is what gets stored and what the model is given, whether they typed it or
 * corrected it.
 *
 * Which controls appear is `jd_intake_mode` from `GET /config`. The server
 * enforces the same rule independently, so this only decides what to render —
 * a stale tab offering the wrong control gets a sentence back explaining why,
 * rather than a broken form.
 */
export function JdInput({
  value,
  onChange,
}: {
  value: JdValue;
  onChange: (value: JdValue) => void;
}) {
  const { data: config } = useConfig();
  const extract = useExtractJdDocument();
  const [document, setDocument] = useState<JdDocument | null>(null);
  const [problem, setProblem] = useState('');

  const mode = config?.jd_intake_mode ?? 'both';
  const showUpload = mode !== 'paste';
  const showPaste = mode !== 'upload';
  const accept = (config?.allowed_extensions ?? ['.pdf', '.docx']).join(',');
  const maxMb = Math.round((config?.jd_max_file_bytes ?? 2 * 1024 * 1024) / (1024 * 1024));

  function upload(file: File) {
    setProblem('');
    extract.mutate(file, {
      onSuccess: (result) => {
        setDocument(result);
        onChange({
          text: result.text,
          source: 'upload',
          filename: result.filename,
          fileSha256: result.file_sha256,
          ocrUsed: result.ocr_used,
        });
      },
      onError: (error) => {
        setProblem(error.message);
      },
    });
  }

  function clear() {
    setDocument(null);
    setProblem('');
    onChange(emptyJd);
  }

  return (
    <div className="space-y-3">
      {showUpload && !document ? (
        <FileInput
          accept={accept}
          disabled={extract.isPending}
          onSelect={upload}
          hint={`PDF or Word, up to ${String(maxMb)} MB and ${String(config?.jd_max_pages ?? 10)} pages.`}
        />
      ) : null}

      {extract.isPending ? (
        <Alert tone="info">
          <p>Reading the document…</p>
        </Alert>
      ) : null}

      {/*
        The title states that the upload failed and nothing about *why*, because
        this branch catches every failure — the server rejecting the document,
        but equally a 404 from an API that has not been restarted, a 503 while
        it is busy, or a dropped connection. Naming a cause here would tell
        someone their file is bad on the evidence of a network error, and they
        would go and re-export a document that was never the problem. The
        server's own sentence is the only thing that knows.
      */}
      {problem ? (
        <Alert tone="error" title="That upload did not work">
          <p>{problem}</p>
          {showPaste ? <p>You can paste the text below instead.</p> : null}
        </Alert>
      ) : null}

      {document ? (
        <div className="space-y-2">
          <div className="flex items-center gap-2 rounded-lg border border-neutral-200 px-3 py-2 text-sm dark:border-neutral-800">
            <FileText className="size-4 shrink-0 text-neutral-400" aria-hidden />
            <span className="min-w-0 flex-1 truncate">{document.filename}</span>
            <span className="shrink-0 text-xs text-neutral-500 dark:text-neutral-400">
              {document.page_count} {document.page_count === 1 ? 'page' : 'pages'}
            </span>
            <button
              type="button"
              onClick={clear}
              className="shrink-0 rounded p-1 hover:bg-neutral-100 dark:hover:bg-neutral-800"
              aria-label="Remove this document"
            >
              <X className="size-4" aria-hidden />
            </button>
          </div>

          {/*
            Not decoration. A rubric drafted from OCR'd text is drafted from an
            approximation, and this reviewer is the only person positioned to
            notice the approximation dropped something that matters.
          */}
          {document.ocr_used ? (
            <Alert tone="warn" title="This document was read by OCR">
              <p>
                It has no text layer, so the words below were recognised from an image and may be
                wrong. Read them against the original before continuing.
              </p>
            </Alert>
          ) : null}

          {/*
            Advisory, never blocking. The patterns were calibrated against resume
            prose and a job description says several of those things routinely,
            so this points at a paragraph rather than making a claim about it —
            and the reviewer can simply edit the text.
          */}
          {document.injection_signals.length > 0 ? (
            <Alert tone="warn" title="This document contains instruction-like text">
              <p>
                Something in it reads like an instruction to the model rather than a job requirement
                ({document.injection_signals.join(', ')}). Check the text below and remove anything
                that does not belong.
              </p>
            </Alert>
          ) : null}

          {document.warnings.length > 0 ? (
            <Alert tone="info" title="The reader reported">
              <ul className="list-disc space-y-0.5 pl-4">
                {document.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            </Alert>
          ) : null}
        </div>
      ) : null}

      {showPaste || document ? (
        <Field
          label={document ? 'What the reader found — correct anything wrong' : 'Job description'}
          hint={
            document
              ? 'This text is what the rubric is drafted from, not the original file. Edit it freely.'
              : 'The model drafts a rubric from this, which you then edit and approve. Nothing is screened against a rubric a person has not approved.'
          }
        >
          <textarea
            rows={14}
            value={value.text}
            placeholder={
              showPaste
                ? 'Paste the job description. The rubric is drafted from this text.'
                : 'Upload a document above.'
            }
            onChange={(event) => {
              onChange({ ...value, text: event.target.value });
            }}
          />
        </Field>
      ) : null}
    </div>
  );
}
