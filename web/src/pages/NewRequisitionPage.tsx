import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { Link, useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { useCreatePosition } from '../api/queries';
import { FolderPicker } from '../components/FolderPicker';
import { Alert } from '../ui/Alert';
import { Button } from '../ui/Button';
import { Card, CardBody, CardHeader } from '../ui/Card';
import { Field } from '../ui/Field';

interface Fields {
  title: string;
  jd_text: string;
}

/**
 * Raise a requisition: a folder on the share, a title, and the job description.
 *
 * The folder is chosen first because it is the part people get wrong. The other
 * two fields are typed from something already written; the folder has to be found
 * on a server the reviewer cannot see, and a requisition pointed at the wrong one
 * screens the wrong applicants without ever looking broken.
 */
export function NewRequisitionPage() {
  const [folder, setFolder] = useState('');
  const [folderProblem, setFolderProblem] = useState('');
  const create = useCreatePosition();
  const navigate = useNavigate();
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<Fields>({ defaultValues: { title: '', jd_text: '' } });

  const onSubmit = handleSubmit((values) => {
    // Checked on submit rather than by disabling the button, so the reason is
    // stated. A disabled control with no explanation is a dead end.
    if (!folder) {
      setFolderProblem('Choose the resume folder this job posting screens.');
      return;
    }
    setFolderProblem('');
    create.mutate(
      { reference: folder, title: values.title.trim(), jd_text: values.jd_text.trim() },
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

            <Field
              label="Job description"
              hint="The model drafts a rubric from this, which you then edit and approve. Nothing is screened against a rubric a person has not approved."
            >
              <textarea
                rows={14}
                placeholder="Paste the job description. The rubric is drafted from this text."
                {...register('jd_text', {
                  validate: (value) => value.trim().length > 0 || 'A job description is required.',
                })}
              />
            </Field>

            {folderProblem ? (
              <Alert tone="error">
                <p>{folderProblem}</p>
              </Alert>
            ) : null}
            {(errors.title ?? errors.jd_text) ? (
              <Alert tone="error">
                <p>{errors.title?.message ?? errors.jd_text?.message}</p>
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
