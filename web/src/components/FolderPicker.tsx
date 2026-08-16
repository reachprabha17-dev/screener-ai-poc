import { ChevronLeft, ChevronRight, FolderOpen, RefreshCw } from 'lucide-react';
import { useState } from 'react';
import { folderPageSize, useFolders } from '../api/queries';
import { useDebounced } from '../lib/useDebounced';
import { Alert } from '../ui/Alert';
import { Button } from '../ui/Button';
import { QueryState } from './QueryState';

/**
 * Navigate the resume share and pick the folder a requisition screens.
 *
 * **The server's filesystem, not the reviewer's.** The share is mounted on the
 * screener host, so browsing here is browsing that share. A browser cannot hand a
 * server a path from the machine it is running on — which is why this is a
 * server-side navigator rather than a native folder dialog, and why the reviewer
 * never sees or types an absolute path. Paths are relative to `RESUMES_DIR`
 * throughout, and the server re-checks containment on every use regardless of
 * what this widget produced.
 */
export function FolderPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (path: string) => void;
}) {
  const [path, setPath] = useState('');
  const [typed, setTyped] = useState('');
  const [offset, setOffset] = useState(0);
  const search = useDebounced(typed);
  const page = useFolders(path, search, offset);

  function goTo(next: string): void {
    setPath(next);
    setOffset(0);
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm text-neutral-500 dark:text-neutral-400">
          In: <code>{path || 'share root'}</code>
        </span>
        <div className="flex gap-2">
          {path ? (
            <>
              <Button
                size="sm"
                onClick={() => {
                  goTo(path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '');
                }}
              >
                Up one level
              </Button>
              {/* Selecting where you *are*, not only what is below it: a
                  RESUMES_DIR pointing straight at a folder of CVs otherwise
                  offers nothing to pick. */}
              <Button
                size="sm"
                onClick={() => {
                  onChange(path);
                }}
              >
                Use this folder
              </Button>
            </>
          ) : null}
          <Button
            size="sm"
            variant="ghost"
            onClick={() => void page.refetch()}
            title="Re-read the share — use after adding a folder"
          >
            <RefreshCw className="size-3.5" aria-hidden />
            Refresh
          </Button>
        </div>
      </div>

      <input
        value={typed}
        placeholder="Type to narrow the list…"
        aria-label="Search folders"
        onChange={(event) => {
          setTyped(event.target.value);
          setOffset(0);
        }}
      />

      <QueryState query={page} loading="Reading the share…">
        {(data) => (
          <>
            {data.folders.length === 0 ? (
              <Emptiness path={path} search={search} />
            ) : (
              <ul className="divide-y divide-neutral-100 rounded-lg border border-neutral-200 dark:divide-neutral-800 dark:border-neutral-800">
                {data.folders.map((folder) => (
                  <li key={folder.path} className="flex items-center gap-3 px-3 py-2">
                    <FolderOpen className="size-4 shrink-0 text-neutral-400" aria-hidden />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-medium">{folder.name}</span>
                      <span className="text-xs text-neutral-500 dark:text-neutral-400">
                        {folder.file_count} CV(s)
                      </span>
                    </span>
                    {folder.has_subfolders ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          goTo(folder.path);
                        }}
                      >
                        Open
                      </Button>
                    ) : null}
                    <Button
                      size="sm"
                      variant="primary"
                      onClick={() => {
                        onChange(folder.path);
                      }}
                    >
                      Use
                    </Button>
                  </li>
                ))}
              </ul>
            )}

            {data.total > folderPageSize ? (
              <div className="flex items-center justify-between text-sm">
                <Button
                  size="sm"
                  disabled={offset === 0}
                  onClick={() => {
                    setOffset(Math.max(0, offset - folderPageSize));
                  }}
                >
                  <ChevronLeft className="size-3.5" aria-hidden /> Prev
                </Button>
                <span className="text-neutral-500 dark:text-neutral-400">
                  {offset + 1}–{offset + data.folders.length} of {data.total}
                </span>
                <Button
                  size="sm"
                  disabled={offset + folderPageSize >= data.total}
                  onClick={() => {
                    setOffset(offset + folderPageSize);
                  }}
                >
                  Next <ChevronRight className="size-3.5" aria-hidden />
                </Button>
              </div>
            ) : null}
          </>
        )}
      </QueryState>

      {value ? (
        <Alert tone="ok">
          <p>
            Screening <code>{value}</code>
          </p>
        </Alert>
      ) : null}
    </div>
  );
}

function Emptiness({ path, search }: { path: string; search: string }) {
  if (search) {
    return (
      <p className="py-6 text-center text-sm text-neutral-500 dark:text-neutral-400">
        No folders matching “{search}”.
      </p>
    );
  }
  if (path) {
    return (
      <p className="py-6 text-center text-sm text-neutral-500 dark:text-neutral-400">
        No subfolders here — use <strong>Use this folder</strong> to screen this one.
      </p>
    );
  }
  return (
    <Alert tone="warn">
      <p>
        Nothing found on the resume share. Either it holds no folders yet, or{' '}
        <code>RESUMES_DIR</code> is not pointing where you think — it is currently a folder the
        server can see but which is empty. Add a folder of CVs inside it, then use{' '}
        <strong>Refresh</strong>.
      </p>
    </Alert>
  );
}
