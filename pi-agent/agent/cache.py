"""
Guardian Shield — Local Screenshot Cache
Saves screenshots locally when the server is unreachable.
Uploads cached screenshots when connection is restored.
"""

import os
import json
import time
import logging
import threading
from pathlib import Path
from typing import Optional, Callable
from datetime import datetime

logger = logging.getLogger("guardian.cache")

CACHE_DIR = Path.home() / ".guardian-shield" / "cache" / "screenshots"
CACHE_INDEX = CACHE_DIR / "_index.json"
MAX_CACHE_SIZE_MB = 500  # Max disk usage for cached screenshots
MAX_CACHE_FILES = 5000   # Max number of cached files


class ScreenshotCache:
    """
    Disk-based screenshot queue for offline mode.
    Saves JPEG screenshots to disk when offline, uploads them when online.
    """

    def __init__(self, cache_dir: Optional[Path] = None, max_size_mb: int = MAX_CACHE_SIZE_MB):
        self.cache_dir = cache_dir or CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self._lock = threading.Lock()
        self._pending_count = 0
        self._update_pending_count()

    def _update_pending_count(self):
        """Count files in cache directory."""
        try:
            self._pending_count = len(list(self.cache_dir.glob("*.jpg")))
        except Exception:
            self._pending_count = 0

    def save(self, jpeg_bytes: bytes, timestamp: float) -> bool:
        """
        Save a screenshot to the local cache.

        Args:
            jpeg_bytes: JPEG image data
            timestamp: Unix timestamp of capture

        Returns:
            True if saved successfully
        """
        with self._lock:
            try:
                # Check cache limits
                if self._pending_count >= MAX_CACHE_FILES:
                    self._evict_oldest()

                cache_size = self._get_cache_size()
                if cache_size + len(jpeg_bytes) > self.max_size_bytes:
                    self._evict_oldest()

                # Save with timestamp-based filename
                ts_str = datetime.fromtimestamp(timestamp).strftime("%Y%m%d_%H%M%S_%f")
                filename = f"{ts_str}.jpg"
                filepath = self.cache_dir / filename

                filepath.write_bytes(jpeg_bytes)

                # Save metadata
                meta_path = filepath.with_suffix(".meta")
                meta_path.write_text(json.dumps({
                    "timestamp": timestamp,
                    "size": len(jpeg_bytes),
                    "captured_at": datetime.fromtimestamp(timestamp).isoformat(),
                }), encoding="utf-8")

                self._pending_count += 1
                logger.debug(f"Cached screenshot: {filename} ({len(jpeg_bytes)/1024:.0f} KB)")
                return True

            except Exception as e:
                logger.error(f"Failed to cache screenshot: {e}")
                return False

    def get_pending(self, batch_size: int = 10) -> list:
        """
        Get a batch of cached screenshots for upload.

        Returns:
            List of dicts: [{filepath, jpeg_bytes, timestamp, meta_path}]
        """
        with self._lock:
            result = []
            try:
                files = sorted(self.cache_dir.glob("*.jpg"))[:batch_size]
                for filepath in files:
                    meta_path = filepath.with_suffix(".meta")
                    try:
                        jpeg_bytes = filepath.read_bytes()
                        meta = {}
                        if meta_path.exists():
                            meta = json.loads(meta_path.read_text(encoding="utf-8"))

                        result.append({
                            "filepath": filepath,
                            "meta_path": meta_path,
                            "jpeg_bytes": jpeg_bytes,
                            "timestamp": meta.get("timestamp", filepath.stat().st_mtime),
                        })
                    except Exception as e:
                        logger.warning(f"Failed to read cached file {filepath}: {e}")
                        # Remove corrupt file
                        self._safe_delete(filepath)
                        self._safe_delete(meta_path)

            except Exception as e:
                logger.error(f"Failed to get pending screenshots: {e}")

            return result

    def mark_uploaded(self, filepath: Path, meta_path: Optional[Path] = None):
        """Remove a cached screenshot after successful upload."""
        with self._lock:
            self._safe_delete(filepath)
            if meta_path:
                self._safe_delete(meta_path)
            self._pending_count = max(0, self._pending_count - 1)

    def _evict_oldest(self):
        """Remove the oldest cached screenshot to make room."""
        try:
            files = sorted(self.cache_dir.glob("*.jpg"))
            if files:
                oldest = files[0]
                self._safe_delete(oldest)
                self._safe_delete(oldest.with_suffix(".meta"))
                self._pending_count = max(0, self._pending_count - 1)
                logger.debug(f"Evicted oldest cache: {oldest.name}")
        except Exception as e:
            logger.warning(f"Failed to evict cached file: {e}")

    def _get_cache_size(self) -> int:
        """Get total size of cached files in bytes."""
        try:
            return sum(f.stat().st_size for f in self.cache_dir.glob("*") if f.is_file())
        except Exception:
            return 0

    @staticmethod
    def _safe_delete(path: Path):
        """Delete a file safely, ignoring errors."""
        try:
            if path and path.exists():
                path.unlink()
        except Exception:
            pass

    @property
    def pending_count(self) -> int:
        return self._pending_count

    @property
    def cache_size_mb(self) -> float:
        return self._get_cache_size() / (1024 * 1024)

    def clear(self):
        """Clear all cached screenshots."""
        with self._lock:
            for f in self.cache_dir.glob("*"):
                self._safe_delete(f)
            self._pending_count = 0
            logger.info("Cache cleared")


class CacheUploader:
    """
    Background thread that uploads cached screenshots when server is reachable.
    """

    def __init__(self, cache: ScreenshotCache, upload_fn: Callable, check_interval: float = 10.0):
        """
        Args:
            cache: ScreenshotCache instance
            upload_fn: Function(jpeg_bytes, timestamp) -> bool. Returns True on success.
            check_interval: How often to check for pending uploads (seconds)
        """
        self.cache = cache
        self.upload_fn = upload_fn
        self.check_interval = check_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="cache-uploader")
        self._thread.start()
        logger.info("Cache uploader started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        while self._running:
            if self.cache.pending_count > 0:
                self._upload_batch()
            time.sleep(self.check_interval)

    def _upload_batch(self):
        """Try uploading a batch of cached screenshots."""
        pending = self.cache.get_pending(batch_size=5)
        uploaded = 0

        for item in pending:
            if not self._running:
                break
            try:
                success = self.upload_fn(item["jpeg_bytes"], item["timestamp"])
                if success:
                    self.cache.mark_uploaded(item["filepath"], item.get("meta_path"))
                    uploaded += 1
                else:
                    # Server down, stop trying this batch
                    break
            except Exception as e:
                logger.warning(f"Cache upload failed: {e}")
                break

        if uploaded > 0:
            logger.info(
                f"Uploaded {uploaded} cached screenshots "
                f"({self.cache.pending_count} remaining)"
            )


if __name__ == "__main__":
    # Quick test
    logging.basicConfig(level=logging.DEBUG)
    cache = ScreenshotCache()
    print(f"Cache dir: {cache.cache_dir}")
    print(f"Pending: {cache.pending_count}")
    print(f"Size: {cache.cache_size_mb:.1f} MB")

    # Save a test file
    test_data = b"\xff\xd8\xff\xe0" + b"\x00" * 1000  # fake JPEG
    cache.save(test_data, time.time())
    print(f"After save — Pending: {cache.pending_count}")

    # Get pending
    pending = cache.get_pending()
    print(f"Got {len(pending)} pending items")

    # Clear
    cache.clear()
    print(f"After clear — Pending: {cache.pending_count}")
