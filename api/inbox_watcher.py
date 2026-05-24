"""
inbox_watcher.py — Asyncio polling watcher for the inbox folder.

Monitors a directory for new files, waits for stability (file size unchanged),
then ingests them via the shared pipeline. Processed files are moved to
inbox/processed/, failures to inbox/failed/.
"""

import asyncio
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from config import get_settings
from ingest import ingest_file

logger = logging.getLogger(__name__)

# Track watcher stats (module-level for status endpoint)
_stats = {
    "running": False,
    "files_ingested": 0,
    "files_failed": 0,
    "last_ingested_file": None,
    "last_ingested_at": None,
    "last_error": None,
    "last_error_at": None,
}


def get_inbox_stats() -> dict:
    """Return current inbox watcher statistics."""
    settings = get_settings()
    inbox_dir = Path(settings.inbox_dir)
    pending = []
    if inbox_dir.exists():
        pending = [f.name for f in inbox_dir.iterdir()
                   if f.is_file() and not f.name.startswith(".")]
    return {
        **_stats,
        "inbox_dir": str(inbox_dir),
        "pending_files": len(pending),
        "pending_filenames": pending[:20],
        "poll_interval": settings.inbox_poll_interval,
        "stability_seconds": settings.inbox_stability_seconds,
    }


# Track file sizes for stability check
_file_sizes: dict[str, tuple[int, float]] = {}  # path -> (size, first_seen_at)


def _is_stable(path: Path, stability_seconds: float) -> bool:
    """Check if a file's size has been unchanged for stability_seconds."""
    key = str(path)
    try:
        current_size = path.stat().st_size
    except OSError:
        _file_sizes.pop(key, None)
        return False

    now = time.monotonic()
    if key not in _file_sizes or _file_sizes[key][0] != current_size:
        _file_sizes[key] = (current_size, now)
        return False

    _, first_seen = _file_sizes[key]
    return (now - first_seen) >= stability_seconds


async def _process_file(file_path: Path, processed_dir: Path, failed_dir: Path):
    """Ingest a single file and move it to processed/ or failed/."""
    filename = file_path.name
    logger.info(f"Inbox: ingesting {filename}")

    try:
        result = await ingest_file(
            str(file_path),
            original_filename=filename,
            source="inbox",
        )
        # Move to processed
        dest = processed_dir / filename
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            dest = processed_dir / f"{stem}_{int(time.time())}{suffix}"
        shutil.move(str(file_path), str(dest))

        _stats["files_ingested"] += 1
        _stats["last_ingested_file"] = filename
        _stats["last_ingested_at"] = datetime.now(timezone.utc).isoformat()
        _file_sizes.pop(str(file_path), None)

        if result.get("skipped"):
            logger.info(f"Inbox: {filename} skipped as exact duplicate of doc {result['id']}")
        else:
            logger.info(f"Inbox: ingested {filename} -> doc {result['id']}")

    except Exception as e:
        logger.error(f"Inbox: failed to ingest {filename}: {e}")
        # Move to failed
        try:
            dest = failed_dir / filename
            if dest.exists():
                stem = dest.stem
                suffix = dest.suffix
                dest = failed_dir / f"{stem}_{int(time.time())}{suffix}"
            shutil.move(str(file_path), str(dest))
        except Exception as move_err:
            logger.error(f"Inbox: failed to move {filename} to failed/: {move_err}")

        _stats["files_failed"] += 1
        _stats["last_error"] = str(e)
        _stats["last_error_at"] = datetime.now(timezone.utc).isoformat()
        _file_sizes.pop(str(file_path), None)


async def watch_inbox():
    """Poll the inbox directory and ingest stable files."""
    settings = get_settings()
    inbox_dir = Path(settings.inbox_dir)
    processed_dir = inbox_dir / "processed"
    failed_dir = inbox_dir / "failed"

    # Ensure directories exist
    inbox_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(exist_ok=True)
    failed_dir.mkdir(exist_ok=True)

    _stats["running"] = True
    logger.info(f"Inbox watcher started: monitoring {inbox_dir} (poll={settings.inbox_poll_interval}s, stability={settings.inbox_stability_seconds}s)")

    try:
        while True:
            try:
                # List files in inbox (skip dotfiles, subdirectories)
                files = sorted(
                    (f for f in inbox_dir.iterdir()
                     if f.is_file() and not f.name.startswith(".")),
                    key=lambda f: f.stat().st_mtime,
                )

                for file_path in files:
                    if _is_stable(file_path, settings.inbox_stability_seconds):
                        await _process_file(file_path, processed_dir, failed_dir)
                        # Process one at a time, then re-poll
                        break

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Inbox watcher poll error: {e}")

            await asyncio.sleep(settings.inbox_poll_interval)

    except asyncio.CancelledError:
        _stats["running"] = False
        logger.info("Inbox watcher stopped")
        raise
