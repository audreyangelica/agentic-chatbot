"""Human-review renderers for canonical meeting transcripts."""

from __future__ import annotations

import csv
from io import StringIO
import textwrap
from typing import Any
from xml.sax.saxutils import escape
import zipfile
from io import BytesIO


def _timestamp(seconds: float | None) -> str:
    if seconds is None:
        return "--:--:--.---"
    milliseconds = round((seconds - int(seconds)) * 1000)
    whole = int(seconds)
    minutes, second = divmod(whole, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02}:{minute:02}:{second:02}.{milliseconds:03}"


def render_text(transcript: dict[str, Any]) -> str:
    lines = []
    for turn in transcript["turns"]:
        lines.append(
            f"[{_timestamp(turn['start'])}–{_timestamp(turn['end'])}] "
            f"Speaker {turn['speaker_id']}: {turn['text']}"
        )
    return "\n".join(lines) + "\n"


def render_markdown(transcript: dict[str, Any]) -> str:
    return f"# Meeting {transcript['meeting_id']}\n\n" + render_text(transcript)


def render_csv(transcript: dict[str, Any]) -> str:
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=["turn_id", "speaker_id", "start", "end", "text", "segment_id"])
    writer.writeheader()
    for turn in transcript["turns"]:
        writer.writerow(
            {
                "turn_id": turn["turn_id"],
                "speaker_id": turn["speaker_id"],
                "start": turn["start"],
                "end": turn["end"],
                "text": turn["text"],
                "segment_id": turn["provenance"]["segment_id"],
            }
        )
    return stream.getvalue()


def render_docx(transcript: dict[str, Any]) -> bytes:
    """Create a minimal, dependency-free DOCX review document."""
    paragraphs = [f"Meeting {transcript['meeting_id']}"] + render_text(transcript).rstrip("\n").split("\n")
    body = "".join(f"<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>" for line in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in (
            ("[Content_Types].xml", content_types),
            ("_rels/.rels", relationships),
            ("word/document.xml", document),
        ):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, content)
    return stream.getvalue()


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def render_pdf(transcript: dict[str, Any]) -> bytes:
    """Create a simple, readable PDF without a third-party dependency."""
    lines = [f"Meeting {transcript['meeting_id']}"]
    for line in render_text(transcript).replace("–", "-").rstrip("\n").split("\n"):
        lines.extend(textwrap.wrap(line, width=94, break_long_words=False) or [""])
    pages = [lines[index : index + 46] for index in range(0, len(lines), 46)] or [[]]
    objects: list[bytes] = []
    page_ids = []
    # Catalog and Pages are objects 1 and 2. Font is object 3.
    objects.extend([b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"])
    for page in pages:
        commands = ["BT", "/F1 10 Tf", "50 760 Td", "13 TL"]
        for index, line in enumerate(page):
            if index:
                commands.append("T*")
            commands.append(f"({_pdf_escape(line.encode('latin-1', 'replace').decode('latin-1'))}) Tj")
        commands.append("ET")
        stream = "\n".join(commands).encode("latin-1")
        content_id = len(objects) + 2
        page_id = len(objects) + 1
        page_ids.append(page_id)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode(
                "ascii"
            )
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream")
    objects[1] = f"<< /Type /Pages /Count {len(page_ids)} /Kids [{' '.join(f'{page_id} 0 R' for page_id in page_ids)}] >>".encode("ascii")
    body = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, value in enumerate(objects, start=1):
        offsets.append(len(body))
        body.extend(f"{object_id} 0 obj\n".encode("ascii"))
        body.extend(value)
        body.extend(b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    body.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return bytes(body)
