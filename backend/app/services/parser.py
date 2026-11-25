"""
Document parsing service.
Handles: PDF (docling with pdfplumber fallback), XLSX (openpyxl), DOCX (python-docx), images (OCR via Tesseract).
Produces: list of DocChunkCreate with page_number, section_heading, content, char_offsets.
"""
import concurrent.futures
import io
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DOCLING_ARTIFACTS_PATH = os.environ.get("DOCLING_ARTIFACTS_PATH", "").strip()
DOCLING_CONVERT_TIMEOUT_SEC = int(os.environ.get("DOCLING_CONVERT_TIMEOUT_SEC", "180"))
USE_DOCLING = os.environ.get("USE_DOCLING", "true").lower() in ("1", "true", "yes")

_converter_lock = threading.Lock()
_converter = None


@dataclass
class ParsedChunk:
    page_number: int
    section_heading: str
    content: str
    char_offset_start: int
    char_offset_end: int
    bounding_box: Optional[dict] = None
    source_type: str = "text"   # text | ocr | table


@dataclass
class ParsedDocument:
    file_name: str
    file_type: str
    page_count: int
    chunks: list[ParsedChunk] = field(default_factory=list)
    raw_text: str = ""


def _split_text_if_needed(text: str, limit: int = 10000) -> list[str]:
    """Helper to split a long string into chunks of at most 'limit' characters."""
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def _docling_importable() -> bool:
    if not USE_DOCLING:
        return False
    try:
        import docling.document_converter  # noqa: F401
        return True
    except ImportError:
        return False


def _get_docling_converter():
    """Return a process-wide DocumentConverter (models loaded once)."""
    global _converter
    if _converter is not None:
        return _converter

    with _converter_lock:
        if _converter is not None:
            return _converter

        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        if DOCLING_ARTIFACTS_PATH and os.path.isdir(DOCLING_ARTIFACTS_PATH):
            logger.info("Initializing Docling with local artifacts at %s", DOCLING_ARTIFACTS_PATH)
            pipeline_options = PdfPipelineOptions(artifacts_path=DOCLING_ARTIFACTS_PATH)
            _converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
                }
            )
        else:
            if DOCLING_ARTIFACTS_PATH:
                logger.warning(
                    "DOCLING_ARTIFACTS_PATH=%s does not exist; Docling may download models at runtime",
                    DOCLING_ARTIFACTS_PATH,
                )
            _converter = DocumentConverter()

        return _converter


def warmup_docling(timeout_sec: int | None = None) -> bool:
    """Load Docling models at startup so uploads do not block on Hugging Face downloads."""
    if not _docling_importable():
        logger.info("Docling disabled or not installed; PDF parsing will use pdfplumber")
        return False

    if timeout_sec is None:
        timeout_sec = int(os.environ.get("DOCLING_WARMUP_TIMEOUT_SEC", "120"))

    try:
        logger.info("Warming up Docling converter (timeout=%ss) ...", timeout_sec)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_get_docling_converter)
            future.result(timeout=timeout_sec)
        logger.info("Docling converter ready")
        return True
    except concurrent.futures.TimeoutError:
        logger.warning(
            "Docling warmup timed out after %ss; PDFs will fall back to pdfplumber",
            timeout_sec,
        )
        return False
    except Exception as exc:
        logger.warning("Docling warmup failed (%s); PDFs will fall back to pdfplumber", exc)
        return False


def _docling_convert_file(tmp_path: str) -> str:
    converter = _get_docling_converter()
    result = converter.convert(tmp_path)
    return result.document.export_to_markdown()


def _markdown_to_chunks(markdown_text: str) -> list[ParsedChunk]:
    chunks: list[ParsedChunk] = []
    offset = 0

    sections = []
    current_heading = "Introduction"
    current_buf: list[str] = []

    for line in markdown_text.split("\n"):
        if line.startswith("## ") or line.startswith("### "):
            if current_buf:
                sections.append((current_heading, "\n".join(current_buf)))
            current_heading = line.lstrip("#").strip()
            current_buf = [line]
        else:
            current_buf.append(line)

    if current_buf:
        sections.append((current_heading, "\n".join(current_buf)))

    for heading, content in sections:
        for i, part in enumerate(_split_text_if_needed(content, limit=4000)):
            chunks.append(ParsedChunk(
                page_number=1,
                section_heading=heading + (f" (Part {i + 1})" if i > 0 else ""),
                content=part,
                char_offset_start=offset,
                char_offset_end=offset + len(part),
                source_type="text",
            ))
            offset += len(part) + 1

    return chunks


# ── PDF parser ─────────────────────────────────────────────────────────────────
def _parse_pdf_with_pdfplumber(file_bytes: bytes, file_name: str) -> ParsedDocument:
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("PDF parsing requires docling or pdfplumber")

    chunks: list[ParsedChunk] = []
    raw_pages: list[str] = []
    offset = 0

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page_idx, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            raw_pages.append(text)
            if not text.strip():
                continue

            for part_idx, part in enumerate(_split_text_if_needed(text, limit=4000)):
                chunks.append(ParsedChunk(
                    page_number=page_idx,
                    section_heading=f"Page {page_idx}" + (f" (Part {part_idx + 1})" if part_idx > 0 else ""),
                    content=part,
                    char_offset_start=offset,
                    char_offset_end=offset + len(part),
                    source_type="text",
                ))
                offset += len(part) + 1

        page_count = len(pdf.pages)

    raw_text = "\n\n".join(raw_pages)
    return ParsedDocument(
        file_name=file_name,
        file_type="pdf",
        page_count=page_count,
        chunks=chunks,
        raw_text=raw_text,
    )


def _norm_for_match(text: str) -> str:
    """Lower-case alphanumerics only, so Markdown and PDF text can be compared."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _pdf_page_texts(file_bytes: bytes) -> list[str]:
    """Normalised text of every page (empty list if the PDF cannot be read)."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            return [_norm_for_match(p.extract_text() or "") for p in pdf.pages]
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not read page texts for page attribution: %s", exc)
        return []


def _locate_page(block: str, pages: list[str], start: int) -> Optional[int]:
    """
    0-based index of the page that contains ``block``, searching from ``start``
    onwards (documents are read in order) and then from the beginning. Several
    probes are tried because tables and headings are rendered differently in
    Markdown and in the PDF text layer.
    """
    norm = _norm_for_match(block)
    if len(norm) < 12:
        return None
    probes = [norm[:60], norm[len(norm) // 2: len(norm) // 2 + 40], norm[-40:]]
    order = list(range(start, len(pages))) + list(range(0, start))
    for probe in probes:
        if len(probe) < 12:
            continue
        for idx in order:
            if probe in pages[idx]:
                return idx
    return None


def _markdown_to_paged_chunks(markdown_text: str, pages: list[str]) -> list[ParsedChunk]:
    """
    Split Docling Markdown into chunks that never cross a page boundary.

    Docling's Markdown export has no page numbers, so every chunk used to be
    recorded on page 1 and every citation pointed at the first page. Each
    paragraph (or table) is located in the PDF's own text layer and consecutive
    paragraphs of the same section and page are grouped into one chunk.
    """
    chunks: list[ParsedChunk] = []
    offset = 0
    heading = "Introduction"
    page_idx = 0
    buf: list[str] = []
    buf_page = 0
    buf_heading = heading

    def flush():
        nonlocal offset, buf
        content = "\n\n".join(buf).strip()
        if content:
            for i, part in enumerate(_split_text_if_needed(content, limit=4000)):
                chunks.append(ParsedChunk(
                    page_number=buf_page + 1,
                    section_heading=buf_heading + (f" (Part {i + 1})" if i > 0 else ""),
                    content=part,
                    char_offset_start=offset,
                    char_offset_end=offset + len(part),
                    source_type="text",
                ))
                offset += len(part) + 1
        buf = []

    for block in re.split(r"\n\s*\n", markdown_text):
        block = block.strip()
        if not block:
            continue
        if block.startswith("#"):
            first_line = block.split("\n", 1)[0]
            located = _locate_page(first_line.lstrip("#"), pages, page_idx)
            flush()
            heading = first_line.lstrip("#").strip() or heading
            if located is not None:
                page_idx = located
            buf_heading, buf_page = heading, page_idx
            buf = [block]
            continue
        located = _locate_page(block, pages, page_idx)
        if located is not None and located != buf_page and buf:
            flush()
            buf_heading = heading
        if located is not None:
            page_idx = located
        if not buf:
            buf_page, buf_heading = page_idx, heading
        buf.append(block)
    flush()
    return chunks


def _parse_pdf_with_docling(file_bytes: bytes, file_name: str) -> ParsedDocument:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_docling_convert_file, tmp_path)
            try:
                markdown_text = future.result(timeout=DOCLING_CONVERT_TIMEOUT_SEC)
            except concurrent.futures.TimeoutError as exc:
                raise TimeoutError(
                    f"Docling conversion timed out after {DOCLING_CONVERT_TIMEOUT_SEC}s"
                ) from exc
    finally:
        os.remove(tmp_path)

    pages = _pdf_page_texts(file_bytes)
    chunks = (
        _markdown_to_paged_chunks(markdown_text, pages)
        if pages else _markdown_to_chunks(markdown_text)
    )
    return ParsedDocument(
        file_name=file_name,
        file_type="pdf",
        page_count=max(len(pages), 1),
        chunks=chunks,
        raw_text=markdown_text,
    )


def parse_pdf(file_bytes: bytes, file_name: str) -> ParsedDocument:
    if not _docling_importable():
        logger.info("Using pdfplumber for %s (Docling disabled/unavailable)", file_name)
        return _parse_pdf_with_pdfplumber(file_bytes, file_name)

    try:
        return _parse_pdf_with_docling(file_bytes, file_name)
    except Exception as exc:
        logger.warning(
            "Docling failed for %s (%s); falling back to pdfplumber",
            file_name,
            exc,
        )
        return _parse_pdf_with_pdfplumber(file_bytes, file_name)


# ── XLSX parser ────────────────────────────────────────────────────────────────
def parse_xlsx(file_bytes: bytes, file_name: str) -> ParsedDocument:
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("openpyxl not installed")

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    chunks: list[ParsedChunk] = []
    offset = 0

    for sheet_idx, ws in enumerate(wb.worksheets):
        rows_text: list[str] = []
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) if c is not None else "" for c in row]
            if any(c.strip() for c in cells):
                rows_text.append("\t".join(cells))

        if not rows_text:
            continue

        chunk_size_limit = 10000
        current_buf = []
        current_len = 0
        chunk_idx = 0

        header = f"[SHEET: {ws.title}]"

        for row_str in rows_text:
            if len(row_str) > chunk_size_limit:
                if current_buf:
                    content = header + "\n" + "\n".join(current_buf)
                    chunks.append(ParsedChunk(
                        page_number=sheet_idx + 1,
                        section_heading=f"{ws.title} (Part {chunk_idx + 1})",
                        content=content,
                        char_offset_start=offset,
                        char_offset_end=offset + len(content),
                        source_type="table",
                    ))
                    offset += len(content) + 1
                    current_buf = []
                    current_len = 0
                    chunk_idx += 1

                for part in _split_text_if_needed(row_str, chunk_size_limit):
                    content = header + "\n" + part
                    chunks.append(ParsedChunk(
                        page_number=sheet_idx + 1,
                        section_heading=f"{ws.title} (Part {chunk_idx + 1})",
                        content=content,
                        char_offset_start=offset,
                        char_offset_end=offset + len(content),
                        source_type="table",
                    ))
                    offset += len(content) + 1
                    chunk_idx += 1
                continue

            if current_len + len(row_str) > chunk_size_limit and current_buf:
                content = header + "\n" + "\n".join(current_buf)
                chunks.append(ParsedChunk(
                    page_number=sheet_idx + 1,
                    section_heading=f"{ws.title} (Part {chunk_idx + 1})",
                    content=content,
                    char_offset_start=offset,
                    char_offset_end=offset + len(content),
                    source_type="table",
                ))
                offset += len(content) + 1
                current_buf = []
                current_len = 0
                chunk_idx += 1

            current_buf.append(row_str)
            current_len += len(row_str) + 1

        if current_buf:
            content = header + "\n" + "\n".join(current_buf)
            chunks.append(ParsedChunk(
                page_number=sheet_idx + 1,
                section_heading=f"{ws.title}" + (f" (Part {chunk_idx + 1})" if chunk_idx > 0 else ""),
                content=content,
                char_offset_start=offset,
                char_offset_end=offset + len(content),
                source_type="table",
            ))
            offset += len(content) + 1

    return ParsedDocument(
        file_name=file_name,
        file_type="xlsx",
        page_count=len(wb.worksheets),
        chunks=chunks,
    )


# ── DOCX parser ────────────────────────────────────────────────────────────────
def parse_docx(file_bytes: bytes, file_name: str) -> ParsedDocument:
    try:
        from docx import Document as DocxDocument
    except ImportError:
        raise RuntimeError("python-docx not installed")

    doc = DocxDocument(io.BytesIO(file_bytes))
    chunks: list[ParsedChunk] = []
    offset = 0
    current_heading = "Document"
    section_buf: list[str] = []

    def flush(heading: str, buf: list[str], page: int = 1):
        nonlocal offset
        if not buf:
            return

        full_content = "\n".join(buf)
        chunk_size_limit = 10000
        if len(full_content) > chunk_size_limit:
            parts = [full_content[i:i + chunk_size_limit] for i in range(0, len(full_content), chunk_size_limit)]
            for idx, p in enumerate(parts):
                chunks.append(ParsedChunk(
                    page_number=page,
                    section_heading=f"{heading} (Part {idx + 1})",
                    content=p,
                    char_offset_start=offset,
                    char_offset_end=offset + len(p),
                ))
                offset += len(p) + 1
        else:
            chunks.append(ParsedChunk(
                page_number=page,
                section_heading=heading,
                content=full_content,
                char_offset_start=offset,
                char_offset_end=offset + len(full_content),
            ))
            offset += len(full_content) + 1

    for para in doc.paragraphs:
        if para.style and para.style.name.startswith("Heading"):
            flush(current_heading, section_buf)
            section_buf = []
            current_heading = para.text.strip()
        elif para.text.strip():
            section_buf.append(para.text.strip())

    flush(current_heading, section_buf)

    return ParsedDocument(
        file_name=file_name,
        file_type="docx",
        page_count=1,
        chunks=chunks,
    )


# ── OCR parser (images / scanned PDFs) ────────────────────────────────────────
def parse_image_ocr(file_bytes: bytes, file_name: str, page_number: int = 1) -> ParsedDocument:
    """
    Run Tesseract OCR on an image and return a single chunk with full bounding box data.
    For scanned PDFs: caller should convert each page to image then call this.
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        raise RuntimeError("pytesseract and Pillow required: pip install pytesseract Pillow")

    image = Image.open(io.BytesIO(file_bytes))
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)

    words: list[dict] = []
    raw_words: list[str] = []
    for i, word in enumerate(data["text"]):
        if word.strip() and int(data["conf"][i]) > 30:
            words.append({
                "word": word,
                "x": data["left"][i],
                "y": data["top"][i],
                "w": data["width"][i],
                "h": data["height"][i],
                "conf": data["conf"][i],
            })
            raw_words.append(word)

    raw_text = " ".join(raw_words)

    chunks = []
    for i, part in enumerate(_split_text_if_needed(raw_text)):
        chunks.append(ParsedChunk(
            page_number=page_number,
            section_heading="OCR extracted" + (f" (Part {i + 1})" if i > 0 else ""),
            content=part,
            char_offset_start=0,
            char_offset_end=len(part),
            bounding_box={"words": words} if i == 0 else None,
            source_type="ocr",
        ))

    return ParsedDocument(
        file_name=file_name,
        file_type="image",
        page_count=1,
        chunks=chunks,
        raw_text=raw_text,
    )


# ── Router ─────────────────────────────────────────────────────────────────────
def parse_document(file_bytes: bytes, file_name: str) -> ParsedDocument:
    ext = Path(file_name).suffix.lower()
    if ext == ".pdf":
        return parse_pdf(file_bytes, file_name)
    elif ext in (".xlsx", ".xls"):
        return parse_xlsx(file_bytes, file_name)
    elif ext == ".docx":
        return parse_docx(file_bytes, file_name)
    elif ext in (".png", ".jpg", ".jpeg", ".tiff", ".bmp"):
        return parse_image_ocr(file_bytes, file_name)
    else:
        try:
            text = file_bytes.decode("utf-8", errors="replace")
        except Exception:
            text = ""

        chunks: list[ParsedChunk] = []
        offset = 0
        for i, part in enumerate(_split_text_if_needed(text)):
            chunks.append(ParsedChunk(
                page_number=1,
                section_heading="Content" + (f" (Part {i + 1})" if i > 0 else ""),
                content=part,
                char_offset_start=offset,
                char_offset_end=offset + len(part),
            ))
            offset += len(part) + 1

        return ParsedDocument(file_name=file_name, file_type="txt", page_count=1, chunks=chunks, raw_text=text)

