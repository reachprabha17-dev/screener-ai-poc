import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { Link, useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { useCreatePosition } from '../api/queries';
import { FolderPicker } from '../components/FolderPicker';
import { JdInput } from '../components/JdInput';
import { emptyJd, type JdValue } from '../lib/jd';
import { Alert } from '../ui/Alert';
import { Button } from '../ui/Button';
import { Card, CardBody, CardHeader } from '../ui/Card';
import { Field } from '../ui/Field';

interface Fields {
  title: string;
}

/**
 * Raise a requisition: a folder on the share, a title, and the job description.
 *
 * The folder is chosen first because it is the part people get wrong. The title
 * is typed from something already written; the folder has to be found on a server
 * the reviewer cannot see, and a requisition pointed at the wrong one screens the
 * wrong applicants without ever looking broken.
 *
 * The description is `JdInput`, which owns the upload/paste choice and the
 * correction step. This page holds its value and submits it — the provenance
 * travels with the text so the requisition records which document its rubric was
 * drafted from.
 */
export function NewRequisitionPage() {
  const [folder, setFolder] = useState('');
  const [folderProblem, setFolderProblem] = useState('');
  const [jd, setJd] = useState<JdValue>(emptyJd);
  const [jdProblem, setJdProblem] = useState('');
  const create = useCreatePosition();
  const navigate = useNavigate();
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<Fields>({ defaultValues: { title: '' } });

  const onSubmit = handleSubmit((values) => {
    // Checked on submit rather than by disabling the button, so the reason is
    // stated. A disabled control with no explanation is a dead end.
    if (!folder) {
      setFolderProblem('Choose the resume folder this job posting screens.');
      return;
    }
    setFolderProblem('');
    if (!jd.text.trim()) {
      setJdProblem('Upload or paste the job description.');
      return;
    }
    setJdProblem('');
    create.mutate(
      {
        reference: folder,
        title: values.title.trim(),
        jd_text: jd.text.trim(),
        jd_source: jd.source,
        jd_filename: jd.filename,
        jd_file_sha256: jd.fileSha256,
        jd_ocr_used: jd.ocrUsed,
      },
      {
        onSuccess: (position) => {
          toast.success(`Created ${position.reference}`);
          void navigate(`/requisitions/${position.id}`);
        },
        onError: (error) => {
          toast.error(error.message);
        },
      },
    );
  });

  return (
    <div className="space-y-4">
      <p className="text-sm text-neutral-500 dark:text-neutral-400">
        <Link to="/requisitions" className="hover:underline">
          Job Postings
        </Link>{' '}
        / New
      </p>
      <h1 className="text-2xl font-semibold tracking-tight">New job posting</h1>

      <form onSubmit={(event) => void onSubmit(event)} className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="1. Resume folder"
            hint="Folders on the screener host's resume share. The folder name becomes the job posting's reference."
          />
          <CardBody>
            <FolderPicker value={folder} onChange={setFolder} />
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="2. The job" />
          <CardBody>
            <Field label="Job title">
              <input
                placeholder="Senior Platform Engineer"
                {...register('title', {
                  validate: (value) => value.trim().length > 0 || 'A job title is required.',
                })}
              />
            </Field>

            <JdInput value={jd} onChange={setJd} />

            {folderProblem ? (
              <Alert tone="error">
                <p>{folderProblem}</p>
              </Alert>
            ) : null}
            {errors.title ? (
              <Alert tone="error">
                <p>{errors.title.message}</p>
              </Alert>
            ) : null}
            {jdProblem ? (
              <Alert tone="error">
                <p>{jdProblem}</p>
              </Alert>
            ) : null}

            <div className="flex gap-2">
              <Button type="submit" variant="primary" busy={create.isPending}>
                Create job posting
              </Button>
              <Link
                to="/requisitions"
                className="inline-flex items-center rounded-lg border border-neutral-300 px-3.5 py-2 text-sm font-medium hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
              >
                Cancel
              </Link>
            </div>
          </CardBody>
        </Card>
      </form>
    </div>
  );
}
