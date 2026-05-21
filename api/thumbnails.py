"""
thumbnails.py — On-demand document thumbnails with a disk cache.

Renders a small WebP preview for PDFs (page 1 via pdftoppm) and raster
images (via Pillow). Thumbnails are cached under {upload_dir}/thumbnails/
and are fully disposable — they regenerate from the original on demand and
are deliberately excluded from backups.
"""

import logging
import os
import shutil
import signal
import subprocess
import tempfile

from PIL import Image

logger = logging.getLogger(__name__)

# Teach Pillow to decode HEIC/HEIF. Idempotent — processor.py also registers
# it; doing so here too keeps thumbnails working regardless of import order.
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:  # pragma: no cover - dependency always present in the image
    logger.warning("pillow-heif not installed; HEIC/HEIF thumbnails unavailable")

THUMB_WIDTH = 160
THUMB_HEIGHT = 208
THUMB_QUALITY = 80
PDFTOPPM_TIMEOUT = 15  # seconds

# Raster formats Pillow can open (HEIC/HEIF via the pillow-heif opener above).
IMAGE_MIME_TYPES = {
    "image/png", "image/jpeg", "image/tiff", "image/bmp",
    "image/webp", "image/gif", "image/heic", "image/heif",
}


def _cache_dir(upload_dir: str) -> str:
    path = os.path.join(upload_dir, "thumbnails")
    os.makedirs(path, exist_ok=True)
    return path


def _thumbnail_from_image(image_path: str, output_path: str) -> bool:
    """Resize an image down to a WebP thumbnail."""
    try:
        with Image.open(image_path) as img:
            img = img.convert("RGB")
            img.thumbnail((THUMB_WIDTH, THUMB_HEIGHT))
            img.save(output_path, "WEBP", quality=THUMB_QUALITY)
        return True
    except Exception as e:
        logger.warning(f"Thumbnail from image failed for {image_path}: {e}")
        return False


def _thumbnail_from_pdf(pdf_path: str, output_path: str) -> bool:
    """Render page 1 of a PDF with pdftoppm, then resize it to a thumbnail."""
    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="lifeos-thumb-")
        # Run pdftoppm in its own process group so a timeout can kill the
        # whole tree (ghostscript children included) — see CLAUDE.md gotcha #2.
        proc = subprocess.Popen(
            ["pdftoppm", "-png", "-f", "1", "-l", "1", "-r", "150",
             "-scale-to", "320", pdf_path, os.path.join(tmp_dir, "page")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=os.setsid,
        )
        try:
            _, stderr = proc.communicate(timeout=PDFTOPPM_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()
            logger.warning(f"pdftoppm timed out for {pdf_path}")
            return False
        if proc.returncode != 0:
            logger.warning(f"pdftoppm failed for {pdf_path}: {stderr.decode()[:200]}")
            return False

        rendered = next(
            (os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir) if f.endswith(".png")),
            None,
        )
        if not rendered:
            logger.warning(f"pdftoppm produced no output for {pdf_path}")
            return False
        return _thumbnail_from_image(rendered, output_path)
    except Exception as e:
        logger.warning(f"Thumbnail from PDF failed for {pdf_path}: {e}")
        return False
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def get_or_create_thumbnail(
    file_path: str, mime_type: str, doc_id: str, upload_dir: str
) -> str | None:
    """Path to a cached WebP thumbnail for a document, generating on a miss.

    Returns None when the file type isn't previewable or rendering fails —
    the caller should fall back to a generic icon.
    """
    cache_path = os.path.join(_cache_dir(upload_dir), f"{doc_id}.webp")
    if os.path.exists(cache_path):
        return cache_path
    if not file_path or not os.path.exists(file_path):
        return None

    mime = (mime_type or "").lower()
    if mime == "application/pdf":
        ok = _thumbnail_from_pdf(file_path, cache_path)
    elif mime in IMAGE_MIME_TYPES:
        ok = _thumbnail_from_image(file_path, cache_path)
    else:
        ok = False
    return cache_path if ok else None
