"""Document extraction (spec §8, build step 8). **Runs inside the sandbox.**

This module executes in the locked-down subprocess created by `sandbox.py`, with
rlimits applied, a scrubbed environment, and a per-job scratch directory. It is
the only code in the system that touches raw resume bytes, and it is written on
the assumption that the file it is handed is hostile.

It speaks to the parent over exactly one channel: a single JSON object on stdout.
Nothing else — no shared memory, no temp file handoff, no pickle. A parent that
cannot be reached by anything richer than a JSON document cannot be corrupted by
whatever the parser was persuaded to construct.

**The envelope distinguishes two failures that look alike and are not:**

- `ok: false` — the parser read the file, understood it was broken, and said so.
  That is a *deterministic statement about the document*: it will be broken next
  time too, so it is cacheable and it is a data-quality event.
- A non-zero exit or a signal — the parser did not get to say anything. The
  parent classifies that as `PARSER_CRASHED` and treats it as a security event
  (§8.5).

Collapsing those would either page the security team over every corrupt CV, or
bury a genuine exploit attempt in the noise of ordinary bad files.

xberg's API is async-only. The event loop is created and torn down entirely
inside this process, so no async ever reaches the pipeline, the worker, or the
service layer — all of which are synchronous by design (§2).
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

PARSE_ERROR = "parse_error"


def _parser_version() -> str:
    try:
        import xberg

        return f"xberg/{getattr(xberg, '__version__', 'unknown')}"
    except Exception:  # noqa: BLE001 — reported, never raised: this is provenance, not logic
        return "xberg/unavailable"


async def _extract(path: Path) -> dict[str, Any]:
    import xberg
    from xberg.options import ExtractInput

    result = await xberg.extract(ExtractInput(kind="uri", uri=str(path)))
    documents = result.results
    if not documents:
        return {"ok": False, "kind": PARSE_ERROR, "error": "parser returned no document"}

    document = documents[0]
    metadata = document.metadata
    counts = document.counts

    # Two independent signals for OCR, because they disagree by format: the
    # metadata flag is authoritative when present, and `extraction_method` is
    # what actually reports `ocr` on a scanned PDF.
    ocr_used = bool(getattr(metadata, "ocr_used", False)) or (
        str(document.extraction_method or "").casefold() == "ocr"
    )

    return {
        "ok": True,
        "text": document.content or "",
        "page_count": int(getattr(counts, "pages", 0) or 0),
        "ocr_used": ocr_used,
        "warnings": [str(w) for w in (document.processing_warnings or [])],
        "parser_version": _parser_version(),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(json.dumps({"ok": False, "kind": "usage", "error": "expected one path"}))
        return 2

    path = Path(argv[1])
    try:
        payload = asyncio.run(_extract(path))
    except Exception as exc:  # noqa: BLE001 — a hostile file can raise anything at all
        # Caught deliberately rather than allowed to propagate. An unhandled
        # traceback exits non-zero, which the parent must read as a crash — and a
        # merely corrupt CV is not a security event.
        payload = {
            "ok": False,
            "kind": PARSE_ERROR,
            "error": f"{type(exc).__name__}: {exc}"[:2000],
            "parser_version": _parser_version(),
        }

    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
