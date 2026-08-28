"""The sandbox's child, before the parser is (spec 8.4). **Runs unsandboxed, briefly.**

``run_sandboxed`` execs *this* module rather than the parse worker. It applies
the rlimits to itself and then ``execv``s the real worker, which inherits every
one of them: rlimits live in the kernel's per-process signal struct and are not
reset by ``execve``, so the parser is bounded before it reads a byte — exactly as
``preexec_fn`` bounded it.

**Why not ``preexec_fn``.** Everything it ran executed between ``fork`` and
``exec``, the window where only async-signal-safe work is legitimate. That was
survivable while the only caller was the single-threaded worker (16.1). The
job-description upload parses from a FastAPI threadpool handler, and a
``preexec_fn`` in a multi-threaded parent is a latent deadlock: the child
inherits a snapshot of locks other threads may have held at the instant of the
fork, and the CPython runtime the callback needs sits behind some of them. The
symptom is a parse that hangs under load, not one that errors.

**This module imports nothing from this project, and nothing it does not need.**
Everything arrives on argv. Importing ``config.settings`` here would read `.env`
— the database URL, the Ollama host — into the address space of the process
about to be handed a hostile document, which is the whole thing ``_child_env``
exists to prevent. It runs briefly *before* its own limits apply, so keep it
allocating nothing: the only work above ``setrlimit`` is one ``json.loads`` of a
few hundred bytes.

``setsid`` is deliberately **not** here. ``subprocess`` performs it via
``start_new_session=True``, in C between fork and exec, which is async-signal-safe
and covers this process as well as the parser.
"""

import json
import os
import resource
import sys

# Named rather than passed as raw resource numbers: the command line stays
# legible in `ps`, and a typo fails loudly here instead of silently leaving a
# limit unset.
_LIMITS = {
    "AS": resource.RLIMIT_AS,
    "CORE": resource.RLIMIT_CORE,
    "CPU": resource.RLIMIT_CPU,
    "DATA": resource.RLIMIT_DATA,
    "FSIZE": resource.RLIMIT_FSIZE,
    "NOFILE": resource.RLIMIT_NOFILE,
    "NPROC": resource.RLIMIT_NPROC,
}

_USAGE = "rlimit_shim: expected <limits-json> <worker-module> <path>\n"


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        sys.stderr.write(_USAGE)
        return 2

    # Annotated rather than inferred: `json.loads` returns `Any`, and this is
    # the one place a typo in the parent's spec could silently leave a limit
    # unset instead of failing.
    limits: list[tuple[str, int, int]] = json.loads(argv[1])
    worker_module, target = argv[2], argv[3]

    for name, soft, hard in limits:
        resource.setrlimit(_LIMITS[name], (soft, hard))

    # Replaces this process. The limits above, the session created by
    # `start_new_session`, the scrubbed environment and the scratch working
    # directory all carry across.
    os.execv(  # noqa: S606 — fixed argv built by the parent, no shell, no user-controlled program
        sys.executable, [sys.executable, "-m", worker_module, target]
    )
    return 1  # unreachable: execv either does not return or raises


if __name__ == "__main__":
    sys.exit(main(sys.argv))
