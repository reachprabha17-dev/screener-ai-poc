"""Uploaded document → text, as one operation (spec 8, 9.1).

The resume path composes 8.2 validation, the 8.4 sandbox and 8.6 sanitization
itself, inside `pipeline.py`, because it has to interleave them with redaction
and offset mapping — the text the model sees is not the text the reviewer reads,
and the pipeline is what keeps the two addressable. Nothing here needs that.

A job description upload wants the same three steps in the same order and
nothing between them, so they are composed once, here, behind
``ports.DocumentExtractor``. That is what lets `service.py` ask for text without
importing `screener.intake` — which it must not do (4), because the service layer
runs inside the API process and the rule that keeps document parsing out of it is
worth more than the convenience of a direct call. The composition root wires the
concrete class in, exactly as it wires `OllamaClient`.

**The order is the specification, and it is the same order 13 uses.**
`validate_file` before anything opens the file; the sandbox before anything reads
the text; `sanitize` before anything *else* reads the text. Sanitizing last would
mean the caller displays, matches or scans a string that still carries the bidi
overrides and zero-width characters that exist to make a document read one way to
a human and another to a parser (8.6).

This module writes the bytes to a private scratch directory and deletes it. It
takes bytes rather than a path so that no caller above it has to invent a safe
filename for hostile content — that decision is made here, once, next to the
validation that depends on it.
"""

import hashlib
import shutil
import tempfile
import uuid
from pathlib import Path

from config.settings import settings
from screener.intake.sandbox import SandboxedParser
from screener.intake.sanitize_text import sanitize
from screener.intake.validate_file import validate_file
from screener.models import Flag, ParsedResume, ParseResult
from screener.ports import ResumeParser

# Uploaded bytes live here for the length of one parse. Outside `data/`, so
# nothing in the erasure path (12.6) would ever find them — which is why the
# `finally` that removes the directory is the only guarantee there is.
_SCRATCH_PREFIX = "screener-jd-"


class DocumentTextExtractor:
    """Satisfies ``ports.DocumentExtractor``. Validate, parse sandboxed, sanitize.

    ``parser`` is injected rather than constructed so this class can be exercised
    against a fake — the whole point of the port it delegates to. It defaults to
    the real sandbox because every production caller wants that and a required
    argument here would only be ceremony.
    """

    def __init__(self, parser: ResumeParser | None = None) -> None:
        self._parser = parser if parser is not None else SandboxedParser()

    def extract(
        self,
        data: bytes,
        *,
        filename: str,
        timeout_s: int | None = None,
        max_bytes: int | None = None,
        max_pages: int | None = None,
    ) -> ParseResult:
        """Return the document's sanitized text, or the reason there isn't any.

        Returns a ``ParseResult`` rather than raising, for the reason that type
        exists: a document that cannot be read is an ordinary outcome, and the
        caller has to be able to tell a reviewer which of several things went
        wrong.
        """
        suffix = Path(filename).suffix.casefold()
        if suffix not in settings.allowed_extensions:
            # Checked before the file is written, so a name like `payload.sh`
            # never reaches the filesystem with that extension. `validate_file`
            # checks it again against the sniffed type, which is the check that
            # matters; this one only keeps the scratch directory boring.
            return ParseResult(
                flags=[Flag.INPUT_REJECTED],
                error=f"unsupported file type '{suffix or filename}'",
            )

        scratch = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX))
        try:
            # The stem is generated, never taken from the upload: a client-supplied
            # name can be `../../etc/passwd` or absolute. The *suffix* is taken
            # verbatim, and that asymmetry is deliberate — `validate_file` rejects
            # an extension that disagrees with the sniffed container type as a
            # security event, and naming the file after what we sniffed would make
            # that check validate our own sniff against itself.
            target = scratch / f"{uuid.uuid4().hex}{suffix}"
            target.write_bytes(data)

            check = validate_file(target, root=scratch, max_bytes=max_bytes, max_pages=max_pages)
            if not check.ok:
                return ParseResult(
                    flags=[Flag.INPUT_REJECTED],
                    error=str(check.reason or "rejected"),
                )

            result = self._parser.parse(target, timeout_s=timeout_s)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        if result.parsed is None:
            return result

        # 8.6, before the caller sees the string. Everything downstream — what a
        # reviewer reads, what the model is given, what any scan runs over — is
        # this text and not the raw extract.
        cleaned, stripped = sanitize(result.parsed.text)
        return result.model_copy(
            update={
                "parsed": result.parsed.model_copy(
                    update={"text": cleaned, "chars_stripped": stripped}
                )
            }
        )


def sha256_of(data: bytes) -> str:
    """Hash of the uploaded bytes.

    Lives here rather than in `pipeline.file_sha256` because that one hashes a
    file on disk in chunks, for a file this process never holds whole. This one
    is handed the bytes already.
    """
    return hashlib.sha256(data).hexdigest()


__all__ = ["DocumentTextExtractor", "ParsedResume", "sha256_of"]
