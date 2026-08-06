"""Realistic document fixtures for the parse path (build gate §20 step 8).

Separate from `fixtures_files.py`, which builds *malicious* files to be rejected
before parsing. These are documents meant to be read successfully — or to fail in
the specific, well-behaved ways §8.5 distinguishes.

Generated rather than committed, for the same reason: a binary fixture is
unreviewable, and `git diff` cannot show what changed inside a PDF.
"""

import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

RESUME_TEXT = (
    "Asha Nair. Senior Backend Engineer with 7 years of experience. "
    "Built payment systems handling 40 million requests per day in Python and Go."
)

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
    'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
)

_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
    '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
)


def real_docx(path: Path, paragraphs: tuple[str, ...] = (RESUME_TEXT, "2019-2024 Acme")) -> Path:
    """A DOCX whose text xberg actually extracts."""
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("word/document.xml", document)
    return path


def scanned_pdf(path: Path, text: str = RESUME_TEXT, pages: int = 1) -> Path:
    """An image-only PDF — no text layer, so it can only be read by OCR.

    This is the fixture that proves the OCR path runs. Without it, a corpus of
    scanned CVs would extract as empty and the conclusion drawn would be about
    model quality rather than about documents that were never read (§3.3).
    """
    images = []
    for index in range(pages):
        page = Image.new("RGB", (1240, 1754), "white")
        draw = ImageDraw.Draw(page)
        draw.text((80, 120), text, fill="black")
        draw.text((80, 200), f"Page {index + 1}", fill="black")
        images.append(page)

    # `append_images` must be a list — passing None for the single-page case
    # raises rather than being ignored.
    images[0].save(path, "PDF", resolution=150.0, save_all=True, append_images=images[1:])
    return path


def corrupt_pdf(path: Path) -> Path:
    """Valid PDF header, structurally broken body.

    The parser should read it, understand it is broken, and *say so* — a clean
    `parse_error`, not a crash.
    """
    path.write_bytes(b"%PDF-1.4\n" + bytes(range(256)) * 20)
    return path
