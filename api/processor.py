"""
processor.py - Document processing pipeline

Handles text extraction from:
  - PDF (with OCR fallback via ocrmypdf)
  - Images (OCR via Tesseract)
  - Plain text / CSV / JSON
"""

import os
import re
import logging
import subprocess
import tempfile
from pathlib import Path

import chardet

logger = logging.getLogger(__name__)


# ── HEIC/HEIF support ────────────────────────────────────────────────────
# Registering the opener teaches Pillow to decode HEIC/HEIF for the whole
# process — this covers OCR here, thumbnails, and the multi-image merge path.
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:  # pragma: no cover - dependency always present in the image
    logger.warning("pillow-heif not installed; HEIC/HEIF files will not be processed")


# ── OCR Preprocessing ────────────────────────────────────────────────────

def _safe_remove(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def _preprocess_pdf_with_ocrmypdf(file_path: str, *, force: bool = False) -> tuple[str, str | None]:
    """Run ocrmypdf to deskew, auto-rotate, and add text layer.

    ``force=True`` re-rasterises every page and OCRs from scratch — needed
    when an upstream PDF (e.g. mobile capture) has a synthetic text layer of
    positioned glyphs that pdfplumber can technically extract but produces
    garbage. Default (skip-text) is fast for real PDFs.

    Returns (processed_pdf_path, sidecar_text_path) or (original_path, None).
    """
    fd, output_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    fd2, sidecar_path = tempfile.mkstemp(suffix=".txt")
    os.close(fd2)

    text_handling = "--force-ocr" if force else "--skip-text"

    try:
        proc = subprocess.Popen(
            [
                "ocrmypdf",
                text_handling,
                "--deskew",
                "--rotate-pages",
                "--rotate-pages-threshold", "2",
                "-j", "2",
                "-l", "eng",
                "--output-type", "pdf",
                "--sidecar", sidecar_path,
                file_path,
                output_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )

        try:
            _, stderr = proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), 9)
            proc.wait()
            logger.warning(f"ocrmypdf timed out for {file_path}")
            _safe_remove(output_path)
            _safe_remove(sidecar_path)
            return file_path, None

        if proc.returncode in (0, 6):
            return output_path, sidecar_path
        else:
            logger.warning(f"ocrmypdf returned {proc.returncode}: {stderr.decode()[:300]}")
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return output_path, sidecar_path if os.path.exists(sidecar_path) else None
            _safe_remove(output_path)
            _safe_remove(sidecar_path)
            return file_path, None

    except FileNotFoundError:
        logger.error("ocrmypdf not found — is it installed?")
        _safe_remove(output_path)
        _safe_remove(sidecar_path)
        return file_path, None
    except Exception as e:
        logger.warning(f"ocrmypdf error for {file_path}: {e}")
        _safe_remove(output_path)
        _safe_remove(sidecar_path)
        return file_path, None


def _looks_garbled(text: str) -> bool:
    """True when extracted PDF text looks like positioned-glyph garbage —
    almost every line is 1-2 characters because the upstream PDF was
    constructed from per-glyph drawing ops rather than real text runs
    (mobile capture PDFs do this, scanned PDFs sometimes do too).

    Conservative: requires both a meaningful line count AND a high ratio
    of micro-lines, so short legitimate texts (a printed PIN, a receipt
    sliver) don't trigger an expensive force-OCR.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 10:
        return False
    micro_lines = sum(1 for line in lines if len(line) <= 2)
    return micro_lines / len(lines) > 0.40


def _auto_orient_image(file_path: str):
    """Auto-orient image using EXIF data and tesseract OSD."""
    from PIL import Image, ImageOps

    img = Image.open(file_path)
    img = ImageOps.exif_transpose(img)

    try:
        import pytesseract
        osd = pytesseract.image_to_osd(img)
        angle = 0
        for line in osd.split("\n"):
            if "Rotate:" in line:
                angle = int(line.split(":")[1].strip())
                break
        if angle and angle != 0:
            logger.info(f"Tesseract OSD detected {angle}° rotation for {file_path}")
            img = img.rotate(-angle, expand=True)
    except Exception as e:
        logger.debug(f"Tesseract OSD skipped for {file_path}: {e}")

    return img


# ── Text Extraction ──────────────────────────────────────────────────────

def extract_text_from_pdf(file_path: str) -> str:
    """Extract text from PDF, with OCR fallback for scanned documents."""
    import pdfplumber

    processed, sidecar = _preprocess_pdf_with_ocrmypdf(file_path)
    try:
        text_pages = []
        try:
            with pdfplumber.open(processed) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text and text.strip():
                        text_pages.append(text)
        except Exception as e:
            logger.warning(f"pdfplumber failed on {file_path}: {e}")

        if text_pages and sum(len(p) for p in text_pages) > 50:
            return '\n\n'.join(text_pages)

        # Fallback: sidecar text from ocrmypdf
        if sidecar:
            try:
                with open(sidecar, "r", errors="replace") as f:
                    sidecar_text = f.read().strip()
                if len(sidecar_text) > 50:
                    logger.info(f"Using sidecar text for {file_path}")
                    return sidecar_text
            except OSError:
                pass

        # Last resort: direct tesseract OCR
        logger.info(f"OCR fallback on {file_path}")
        return ocr_pdf(processed)
    finally:
        if processed != file_path:
            _safe_remove(processed)
        if sidecar:
            _safe_remove(sidecar)


def ocr_pdf(file_path: str) -> str:
    """OCR a PDF by converting pages to images then running Tesseract."""
    from PIL import Image
    import pytesseract

    all_pages = []
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            subprocess.run(
                ["pdftoppm", "-png", "-r", "150", file_path, f"{tmpdir}/page"],
                check=True, capture_output=True, timeout=120,
            )
        except Exception as e:
            logger.error(f"pdftoppm failed: {e}")
            return ""

        for img_path in sorted(Path(tmpdir).glob("*.png")):
            try:
                img = Image.open(img_path)
                text = pytesseract.image_to_string(img, timeout=60)
                if text.strip():
                    all_pages.append(text.strip())
            except Exception as e:
                logger.warning(f"OCR failed on page {img_path}: {e}")

    return '\n\n'.join(all_pages)


def extract_text_from_image(file_path: str) -> str:
    """OCR text from an image file with auto-orientation."""
    import pytesseract

    try:
        img = _auto_orient_image(file_path)
        # Normalise palette (GIF) and alpha (WEBP/PNG) modes before OCR —
        # Tesseract works most reliably on RGB/grayscale.
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        text = pytesseract.image_to_string(img, timeout=60)
        return text.strip()
    except Exception as e:
        logger.error(f"Image OCR failed: {e}")
        return ""


def extract_text_from_text(file_path: str) -> str:
    """Read plain text file with encoding detection."""
    try:
        with open(file_path, 'rb') as f:
            raw = f.read()
        detected = chardet.detect(raw[:10000])
        enc = detected.get('encoding', 'utf-8') if detected else 'utf-8'
        return raw.decode(enc, errors='replace')
    except Exception as e:
        logger.error(f"Text extraction failed: {e}")
        return ""


def extract_text_with_metadata(file_path: str, mime_type: str = None) -> dict:
    """Extract text and return quality metadata.

    Returns {"text": str, "ocr_applied": bool, "ocr_confidence": float|None, "page_count": int|None}
    """
    ext = Path(file_path).suffix.lower()
    resolved_mime = mime_type or ""

    # PDF
    if resolved_mime == "application/pdf" or ext == ".pdf":
        import pdfplumber

        processed, sidecar = _preprocess_pdf_with_ocrmypdf(file_path)
        forced_processed: str | None = None
        forced_sidecar: str | None = None
        try:
            text_pages = []
            page_count = 0
            try:
                with pdfplumber.open(processed) as pdf:
                    page_count = len(pdf.pages)
                    for page in pdf.pages:
                        text = page.extract_text()
                        if text and text.strip():
                            text_pages.append(text)
            except Exception as e:
                logger.warning(f"pdfplumber failed on {file_path}: {e}")

            combined = '\n\n'.join(text_pages) if text_pages else ''
            if combined and len(combined) > 50 and not _looks_garbled(combined):
                return {
                    "text": _sanitize_text(combined),
                    "ocr_applied": False,
                    "ocr_confidence": None,
                    "page_count": page_count,
                }

            # Garbled positioned-glyph output (common with mobile-capture
            # PDFs wrapping a single rotated photo). Re-run ocrmypdf with
            # --force-ocr so the rasteriser + tesseract produce clean text.
            if combined and _looks_garbled(combined):
                logger.info(
                    f"{file_path}: pdfplumber extracted garbled text "
                    f"({len(combined)} chars, mostly micro-lines) — "
                    "re-running with --force-ocr"
                )
                forced_processed, forced_sidecar = _preprocess_pdf_with_ocrmypdf(
                    file_path, force=True,
                )
                if forced_sidecar:
                    try:
                        with open(forced_sidecar, "r", errors="replace") as f:
                            forced_text = f.read().strip()
                        if len(forced_text) > 50:
                            return {
                                "text": _sanitize_text(forced_text),
                                "ocr_applied": True,
                                "ocr_confidence": None,
                                "page_count": page_count or None,
                            }
                    except OSError:
                        pass

            # Use sidecar text
            if sidecar:
                try:
                    with open(sidecar, "r", errors="replace") as f:
                        sidecar_text = f.read().strip()
                    if len(sidecar_text) > 50:
                        return {
                            "text": _sanitize_text(sidecar_text),
                            "ocr_applied": True,
                            "ocr_confidence": None,
                            "page_count": page_count or None,
                        }
                except OSError:
                    pass

            # Last resort
            text = ocr_pdf(processed)
            return {
                "text": _sanitize_text(text),
                "ocr_applied": True,
                "ocr_confidence": None,
                "page_count": page_count or None,
            }
        finally:
            if processed != file_path:
                _safe_remove(processed)
            if sidecar:
                _safe_remove(sidecar)
            if forced_processed and forced_processed != file_path:
                _safe_remove(forced_processed)
            if forced_sidecar:
                _safe_remove(forced_sidecar)

    # Images
    image_mimes = {
        "image/png", "image/jpeg", "image/tiff", "image/bmp",
        "image/heic", "image/heif", "image/webp", "image/gif",
    }
    image_exts = {
        ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp",
        ".heic", ".heif", ".webp", ".gif",
    }
    if resolved_mime in image_mimes or ext in image_exts:
        text = extract_text_from_image(file_path)
        return {
            "text": _sanitize_text(text),
            "ocr_applied": True,
            "ocr_confidence": None,
            "page_count": 1,
        }

    # Everything else
    text = extract_text(file_path, mime_type)
    return {
        "text": text,
        "ocr_applied": False,
        "ocr_confidence": None,
        "page_count": None,
    }


# ── Routing ──────────────────────────────────────────────────────────────

MIME_EXTRACTORS = {
    'application/pdf': extract_text_from_pdf,
    'image/png': extract_text_from_image,
    'image/jpeg': extract_text_from_image,
    'image/tiff': extract_text_from_image,
    'image/bmp': extract_text_from_image,
    'image/heic': extract_text_from_image,
    'image/heif': extract_text_from_image,
    'image/webp': extract_text_from_image,
    'image/gif': extract_text_from_image,
    'text/plain': extract_text_from_text,
    'text/csv': extract_text_from_text,
    'text/html': extract_text_from_text,
    'application/json': extract_text_from_text,
}

EXT_EXTRACTORS = {
    '.pdf': extract_text_from_pdf,
    '.png': extract_text_from_image,
    '.jpg': extract_text_from_image,
    '.jpeg': extract_text_from_image,
    '.tiff': extract_text_from_image,
    '.tif': extract_text_from_image,
    '.bmp': extract_text_from_image,
    '.heic': extract_text_from_image,
    '.heif': extract_text_from_image,
    '.webp': extract_text_from_image,
    '.gif': extract_text_from_image,
    '.txt': extract_text_from_text,
    '.csv': extract_text_from_text,
    '.log': extract_text_from_text,
    '.md': extract_text_from_text,
    '.json': extract_text_from_text,
}


def _sanitize_text(text: str) -> str:
    """Remove null bytes that PostgreSQL TEXT columns reject."""
    return text.replace('\x00', '') if text else text


def extract_text(file_path: str, mime_type: str = None) -> str:
    """Route to appropriate extractor based on mime type and extension."""
    if mime_type:
        extractor = MIME_EXTRACTORS.get(mime_type)
        if extractor:
            return _sanitize_text(extractor(file_path))

    ext = Path(file_path).suffix.lower()
    extractor = EXT_EXTRACTORS.get(ext)
    if extractor:
        return _sanitize_text(extractor(file_path))

    logger.warning(f"No extractor for {file_path} (mime: {mime_type}, ext: {ext})")
    return ""


# ── Chunking ─────────────────────────────────────────────────────────────

def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200) -> list[dict]:
    """Split text into overlapping chunks for embedding.

    Returns list of {text, chunk_index, char_start, char_end} dicts.
    """
    if not text or not text.strip():
        return []

    text = re.sub(r'\n{3,}', '\n\n', text).strip()

    if len(text) <= chunk_size:
        return [{"text": text, "chunk_index": 0, "char_start": 0, "char_end": len(text)}]

    chunks = []
    start = 0
    idx = 0

    while start < len(text):
        end = start + chunk_size

        if end >= len(text):
            chunks.append({
                "text": text[start:].strip(),
                "chunk_index": idx,
                "char_start": start,
                "char_end": len(text),
            })
            break

        # Find good break point
        search_region = text[end - 100:end + 100]

        para_break = search_region.rfind('\n\n')
        if para_break != -1:
            end = end - 100 + para_break + 2
        else:
            sentence_break = max(
                search_region.rfind('. '),
                search_region.rfind('? '),
                search_region.rfind('! '),
            )
            if sentence_break != -1:
                end = end - 100 + sentence_break + 2
            else:
                word_break = search_region.rfind(' ')
                if word_break != -1:
                    end = end - 100 + word_break + 1

        chunk_text_str = text[start:end].strip()
        if chunk_text_str:
            chunks.append({
                "text": chunk_text_str,
                "chunk_index": idx,
                "char_start": start,
                "char_end": end,
            })

        start = end - overlap
        idx += 1

    return chunks
