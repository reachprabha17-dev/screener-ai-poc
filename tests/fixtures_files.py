"""Builders for the malicious-file corpus (spec §8.2, build gate §20 step 5).

Generated rather than committed. A zip bomb and a traversal archive checked into
a repository are a hazard to every tool that walks the tree — editors, indexers,
CI artifact scanners — and a binary fixture is unreviewable: nobody can tell from
`git diff` what changed inside it. Built here, the attack is the source code.
"""

import zipfile
from pathlib import Path

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
    'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
)

RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
    '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
)

_W = (
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body><w:p><w:r><w:t>{}</w:t></w:r></w:p></w:body></w:document>"
)


def document_xml(body: str = "Asha Nair. Senior Backend Engineer.", *, doctype: str = "") -> str:
    return f'<?xml version="1.0" encoding="UTF-8" standalone="no"?>{doctype}' + _W.format(body)


def write_docx(
    path: Path, *, document: str | None = None, extra: dict[str, str] | None = None
) -> Path:
    """A structurally valid DOCX. ``extra`` adds arbitrary entries by name."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", RELS)
        archive.writestr("word/document.xml", document or document_xml())
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return path


def write_pdf(path: Path, *, pages: int = 1, body: str = "Asha Nair") -> Path:
    """A minimal uncompressed PDF whose page objects are visible in the raw bytes.

    Uncompressed deliberately: this fixture exercises the page pre-filter, which
    only sees what a non-parsing reader can see.
    """
    objects = "".join(
        f"{i + 3} 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
        for i in range(pages)
    )
    kids = " ".join(f"{i + 3} 0 R" for i in range(pages))
    content = (
        f"%PDF-1.4\n"
        f"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        f"2 0 obj<</Type/Pages/Kids[{kids}]/Count {pages}>>endobj\n"
        f"{objects}"
        f"% {body}\n"
        f"trailer<</Root 1 0 R>>\n%%EOF\n"
    )
    path.write_bytes(content.encode("ascii", errors="replace"))
    return path


def write_zip_bomb(path: Path) -> Path:
    """A DOCX entry whose declared expansion ratio is far past the limit.

    Highly compressible filler, so the central directory advertises the ratio
    without the file on disk being large — which is exactly what makes a bomb
    cheap to send and expensive to open.
    """
    return write_docx(path, extra={"word/media/bomb.bin": "A" * (8 * 1024 * 1024)})


def write_zip_slip(path: Path, *, entry: str = "../../../../etc/cron.d/pwn") -> Path:
    """An entry that escapes the extraction directory when written naively."""
    return write_docx(path, extra={entry: "* * * * * root /bin/sh -c :"})


def write_many_entries(path: Path, count: int) -> Path:
    return write_docx(path, extra={f"word/media/img{i}.bin": "x" for i in range(count)})
