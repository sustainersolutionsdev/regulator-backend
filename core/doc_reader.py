"""
core/doc_reader.py - turns an uploaded document into something Gemini can
read, for the AI summaries (T029, DL-19 point 4).

  PDF    sent to Gemini as the file itself (Gemini reads PDFs directly),
         up to MAX_PDF_BYTES.
  DOCX   paragraphs and table cells, as text.
  XLSX   each sheet's non-empty rows, as text (values only, never formulas).
  PPTX   the text on each slide, plus speaker notes.
  TXT    as text (UTF-8, anything unreadable replaced).
  .doc / .xls / .ppt (older Office formats) are NOT read: reading them would
         need a large converter in the server image. The summary names them
         as "not read" so nothing is missed silently.

Text is cut at MAX_TEXT_CHARS per document (about 10,000 tokens); a cut
document is reported as "read in part". The parsing libraries load only
when a document is read, so server start-up is unaffected (Oct 24
cold-start work). Pure functions: no Firestore or Cloud Storage here.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

MAX_TEXT_CHARS = 40_000
MAX_PDF_BYTES = 8 * 1024 * 1024

OLD_OFFICE = {".doc": "Word", ".xls": "Excel", ".ppt": "PowerPoint"}


@dataclass
class ReadResult:
    kind: str                 # "text" | "pdf" | "unreadable"
    text: str = ""
    data: bytes = b""
    partial: bool = False     # text was cut at MAX_TEXT_CHARS
    reason: str = ""          # for "unreadable": why, in plain words
    extra: dict = field(default_factory=dict)


def _ext(filename: str) -> str:
    return "." + filename.rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""


def _cut(text: str) -> ReadResult:
    text = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    if len(text) > MAX_TEXT_CHARS:
        return ReadResult("text", text=text[:MAX_TEXT_CHARS], partial=True)
    if not text:
        return ReadResult("unreadable", reason="no readable text (it may be scanned or image-only)")
    return ReadResult("text", text=text)


def _docx(data: bytes) -> str:
    import docx  # python-docx
    d = docx.Document(io.BytesIO(data))
    lines = [p.text for p in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))
    return "\n".join(lines)


def _xlsx(data: bytes) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines, total = [], 0
    try:
        for ws in wb.worksheets:
            lines.append(f"[Sheet: {ws.title}]")
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v) for v in row]
                if any(c.strip() for c in cells):
                    line = " | ".join(cells).rstrip(" |")
                    lines.append(line)
                    total += len(line)
                    if total > MAX_TEXT_CHARS:
                        return "\n".join(lines)
    finally:
        wb.close()
    return "\n".join(lines)


def _pptx(data: bytes) -> str:
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    lines = []
    for i, slide in enumerate(prs.slides, start=1):
        lines.append(f"[Slide {i}]")
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                lines.append(shape.text_frame.text)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    lines.append(" | ".join(c.text for c in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(f"Notes: {notes}")
    return "\n".join(lines)


def read_document(data: bytes, filename: str) -> ReadResult:
    ext = _ext(filename)
    if ext in OLD_OFFICE:
        return ReadResult("unreadable", reason=f"older {OLD_OFFICE[ext]} format ({ext})")
    if ext == ".pdf":
        if len(data) > MAX_PDF_BYTES:
            return ReadResult("unreadable", reason=f"PDF larger than {MAX_PDF_BYTES // (1024 * 1024)} MB")
        if not data.startswith(b"%PDF"):
            return ReadResult("unreadable", reason="not a valid PDF")
        return ReadResult("pdf", data=data)
    try:
        if ext == ".txt":
            return _cut(data.decode("utf-8", errors="replace"))
        if ext == ".docx":
            return _cut(_docx(data))
        if ext == ".xlsx":
            return _cut(_xlsx(data))
        if ext == ".pptx":
            return _cut(_pptx(data))
    except Exception:  # noqa: BLE001 - a damaged file must not break the summary
        return ReadResult("unreadable", reason="the file could not be opened")
    return ReadResult("unreadable", reason=f"file type {ext or '(none)'} is not read")
