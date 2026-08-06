"""Daemon entry point (spec §4, §16.6).

A shim. The loop itself lives in `screener/worker_loop.py` because only
`screener/` and `config/` go into the wheel — a root-level module is not
importable from an installed environment, and `screener/cli.py` needs the
`Worker` class for its break-glass `work` command.

Keeping this file means the systemd unit and `python worker.py` both still work,
and §4's folder structure is unchanged.
"""

from screener.worker_loop import Worker, build_worker, main

__all__ = ["Worker", "build_worker", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
