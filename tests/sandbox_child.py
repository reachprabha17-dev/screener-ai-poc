"""Deliberately hostile stand-ins for the parser (build gate 20 step 5).

Every guarantee the sandbox makes is about the case where the child misbehaves,
so these are the only inputs that actually test it. Run as
``python -m sandbox_child <mode>`` by ``run_sandboxed``, which passes the file
path as the sole argument — here that argument selects a failure mode.

Each entry below is a real thing a document parser does when handed a malicious
file. `crash` is a segfault in a native decoder; `memhog` is a decompression
bomb; `spin` is a malformed structure that puts the parser in a loop; `sleeper`
is a parser blocked on something that never arrives, which no CPU limit can
catch.
"""

import json
import os
import signal
import sys
import time
from pathlib import Path


def _ok() -> None:
    print(json.dumps({"text": "Asha Nair. Senior Backend Engineer.", "page_count": 1}))


def _spin() -> None:
    while True:
        pow(7, 7, 999_999_937)


def _sleeper() -> None:
    # Burns no CPU at all, so RLIMIT_CPU never fires. Only the parent's wall
    # clock ends this.
    time.sleep(3600)


def _memhog() -> None:
    blocks = []
    while True:
        blocks.append(bytearray(64 * 1024 * 1024))


def _crash() -> None:
    os.kill(os.getpid(), signal.SIGSEGV)


def _garbage() -> None:
    print("Traceback (most recent call last): not json at all")


def _not_an_object() -> None:
    print(json.dumps([1, 2, 3]))


def _nonzero() -> None:
    sys.stderr.write("parser failed on page 3\n")
    sys.exit(3)


def _dump_env() -> None:
    print(json.dumps({"env": sorted(os.environ)}))


def _dump_cwd() -> None:
    Path("scratch.txt").write_text("parser wrote here")
    print(json.dumps({"cwd": str(Path.cwd())}))


def _fork_bomb() -> None:
    forked = 0
    try:
        while True:
            if os.fork() == 0:
                time.sleep(30)
                os._exit(0)
            forked += 1
    except OSError:
        # RLIMIT_NPROC refused the fork, which is the outcome under test.
        print(json.dumps({"forked": forked}))


MODES = {
    "ok": _ok,
    "spin": _spin,
    "sleeper": _sleeper,
    "memhog": _memhog,
    "crash": _crash,
    "garbage": _garbage,
    "not_an_object": _not_an_object,
    "nonzero": _nonzero,
    "dump_env": _dump_env,
    "dump_cwd": _dump_cwd,
    "fork_bomb": _fork_bomb,
}


if __name__ == "__main__":
    MODES[sys.argv[1]]()
