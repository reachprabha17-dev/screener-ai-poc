"""Browsing the resume share (spec intake share).

Lists **one level at a time** rather than walking the whole tree. A mounted
share can be arbitrarily large and is often on network storage where `rglob`
over the root is slow enough to hang a page render; the picker only ever needs
the children of the folder the user is currently looking at.

`file_count` is the exception and is deliberately recursive, because it has to
mean the same thing `jobs_store._eligible_files` will queue — the number shown
next to a folder is a promise about the run that folder is about to produce.
"""

from pathlib import Path

from config.settings import settings
from screener.core.resume_paths import folder_for, is_safe_reference
from screener.models import FolderInfo


def _count_eligible(folder: Path) -> int:
    """Resumes `jobs_store._eligible_files` would queue from this folder.

    Kept in step with that function by hand — the two live in different layers
    and neither may import the other. The invariant is pinned by a test.
    """
    allowed = {ext.casefold() for ext in settings.allowed_extensions}
    count = 0
    try:
        for path in folder.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            if path.name.startswith("."):
                continue
            if path.suffix.casefold() in allowed:
                count += 1
    except OSError:
        # An unreadable subtree on a share is a permissions fact about the mount,
        # not a reason to fail the browse. Report what could be counted.
        return count
    return count


def list_folders(
    subpath: str = "", query: str = "", offset: int = 0, limit: int = 15
) -> tuple[list[FolderInfo], int]:
    """One page of subdirectories of `resumes_dir / subpath`, filtered and sorted.

    **Paged and filtered here rather than in the UI, because the cost is here.**
    `file_count` is a recursive walk, so counting every folder in a 100-folder
    share is thousands of filesystem operations — imperceptible on local disk and
    seconds on an SMB/NFS mount, where every operation is a network round trip.
    Streamlit re-runs its whole script on each interaction, so that cost would be
    paid again on every click. Names come from one `iterdir`; only the folders on
    the page being displayed are counted, which bounds the work per render at
    `limit` walks no matter how large the share is.

    Returns ([], 0) when the path does not exist, is not a directory, or is
    unsafe — an unmounted share must render as an empty browser, never as a
    traceback in the middle of a recruiter's screen.
    """
    if subpath and not is_safe_reference(subpath):
        return [], 0

    try:
        root = folder_for(subpath) if subpath else Path(settings.resumes_dir).resolve()
    except ValueError:
        return [], 0

    if not root.is_dir():
        return [], 0

    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        return [], 0

    matches: list[Path] = []
    needle = query.casefold().strip()
    for entry in entries:
        # Symlinks are not followed, matching `jobs_store._eligible_files`: a
        # symlinked directory could otherwise pull an unrelated tree into a run.
        if entry.is_symlink() or entry.name.startswith("."):
            continue
        if needle and needle not in entry.name.casefold():
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        matches.append(entry)

    total = len(matches)
    page = matches[offset : offset + limit] if limit > 0 else matches

    results: list[FolderInfo] = []
    for entry in page:
        try:
            has_subfolders = any(
                child.is_dir() and not child.is_symlink() and not child.name.startswith(".")
                for child in entry.iterdir()
            )
        except OSError:
            continue

        results.append(
            FolderInfo(
                name=entry.name,
                path=f"{subpath}/{entry.name}" if subpath else entry.name,
                file_count=_count_eligible(entry),
                has_subfolders=has_subfolders,
            )
        )

    return results, total
