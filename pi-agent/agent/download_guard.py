"""
Guardian Shield — Download Guard Module
Watches the Downloads folder for new files and scans them for malware using ClamAV.
Quarantines malicious files and sends alerts.
Works on both Windows and Ubuntu.
"""

import os
import sys
import platform
import logging
import shutil
import time
import subprocess
import threading
from pathlib import Path
from typing import Optional, Callable, List, Dict
from datetime import datetime

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileMovedEvent

logger = logging.getLogger("guardian.download_guard")

# Dangerous file extensions that should be flagged
DANGEROUS_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".com", ".msi", ".scr", ".pif",
    ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
    ".ps1", ".psm1", ".psd1",  # PowerShell
    ".sh", ".bash", ".csh",  # Linux scripts
    ".app", ".dmg",  # macOS
    ".deb", ".rpm",  # Linux packages
    ".jar", ".class",  # Java
    ".reg",  # Windows registry
    ".inf", ".sct", ".hta",
    ".lnk",  # Shortcuts (can be malicious)
}

# Extensions to skip scanning (too large, known safe types)
SKIP_SCAN_EXTENSIONS = {
    ".iso", ".vmdk", ".vdi", ".ova",  # Disk images
    ".mp4", ".mkv", ".avi", ".mov",  # Videos
    ".mp3", ".wav", ".flac", ".aac",  # Audio
}


def _get_download_paths() -> List[str]:
    """Get the default download directories for the current OS/user."""
    paths = []
    home = Path.home()

    if platform.system() == "Windows":
        # Standard Downloads folder
        downloads = home / "Downloads"
        if downloads.exists():
            paths.append(str(downloads))
        # Also check Desktop (common drop target)
        desktop = home / "Desktop"
        if desktop.exists():
            paths.append(str(desktop))
    else:
        # Linux
        downloads = home / "Downloads"
        if downloads.exists():
            paths.append(str(downloads))
        # XDG download dir
        try:
            result = subprocess.run(
                ["xdg-user-dir", "DOWNLOAD"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                xdg_dl = result.stdout.strip()
                if xdg_dl and os.path.exists(xdg_dl) and xdg_dl not in paths:
                    paths.append(xdg_dl)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return paths


def _get_quarantine_path() -> str:
    """Get or create the quarantine directory."""
    if platform.system() == "Windows":
        quarantine = Path.home() / ".guardian-shield" / "quarantine"
    else:
        quarantine = Path.home() / ".guardian-shield" / "quarantine"

    quarantine.mkdir(parents=True, exist_ok=True)
    return str(quarantine)


def _is_clamav_available() -> bool:
    """Check if ClamAV (clamscan) is installed and available."""
    try:
        result = subprocess.run(
            ["clamscan", "--version"],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def scan_file_clamav(filepath: str) -> Dict:
    """
    Scan a file using ClamAV.

    Returns:
        Dict with keys: 'clean' (bool), 'threat' (str or None), 'error' (str or None)
    """
    try:
        result = subprocess.run(
            ["clamscan", "--no-summary", "--infected", filepath],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            return {"clean": True, "threat": None, "error": None}
        elif result.returncode == 1:
            # Virus found
            threat_name = "Unknown"
            for line in result.stdout.splitlines():
                if "FOUND" in line:
                    # Format: "/path/to/file: ThreatName FOUND"
                    parts = line.split(":")
                    if len(parts) >= 2:
                        threat_part = parts[-1].strip()
                        threat_name = threat_part.replace("FOUND", "").strip()
                    break
            return {"clean": False, "threat": threat_name, "error": None}
        else:
            return {"clean": True, "threat": None, "error": result.stderr.strip()}

    except FileNotFoundError:
        return {"clean": True, "threat": None, "error": "ClamAV not installed"}
    except subprocess.TimeoutExpired:
        return {"clean": True, "threat": None, "error": "Scan timed out"}
    except Exception as e:
        return {"clean": True, "threat": None, "error": str(e)}


class DownloadHandler(FileSystemEventHandler):
    """Watches for new files in download directories and scans them."""

    def __init__(
        self,
        quarantine_path: str,
        on_threat: Optional[Callable] = None,
        on_dangerous_ext: Optional[Callable] = None,
        use_clamav: bool = True,
    ):
        """
        Args:
            quarantine_path: Directory to move malicious files to.
            on_threat: Callback(filepath, threat_name) when malware is found.
            on_dangerous_ext: Callback(filepath, extension) when a dangerous file type is downloaded.
            use_clamav: Whether to use ClamAV for scanning.
        """
        super().__init__()
        self.quarantine_path = quarantine_path
        self.on_threat = on_threat
        self.on_dangerous_ext = on_dangerous_ext
        self.use_clamav = use_clamav and _is_clamav_available()
        self._processed: set = set()  # Avoid double-processing

        if not self.use_clamav:
            logger.warning(
                "ClamAV not available. File scanning will be limited to "
                "extension-based checks only. Install ClamAV for full protection."
            )

    def on_created(self, event):
        if not event.is_directory:
            # Small delay to let the file finish downloading
            threading.Timer(2.0, self._process_file, args=[event.src_path]).start()

    def on_moved(self, event):
        if not event.is_directory:
            threading.Timer(2.0, self._process_file, args=[event.dest_path]).start()

    def _process_file(self, filepath: str):
        """Process a newly downloaded file."""
        # Avoid double processing
        if filepath in self._processed:
            return
        self._processed.add(filepath)

        # Clean up old entries to prevent memory leak
        if len(self._processed) > 10000:
            self._processed.clear()

        if not os.path.exists(filepath):
            return

        filename = os.path.basename(filepath)
        ext = os.path.splitext(filename)[1].lower()

        logger.info(f"New file detected: {filename}")

        # Check for dangerous extensions
        if ext in DANGEROUS_EXTENSIONS:
            logger.warning(f"Dangerous file type detected: {filename} ({ext})")
            if self.on_dangerous_ext:
                self.on_dangerous_ext(filepath, ext)

        # Skip scanning for known safe large files
        if ext in SKIP_SCAN_EXTENSIONS:
            logger.debug(f"Skipping scan for known media type: {ext}")
            return

        # Skip very large files (>100MB) to avoid long scan times
        try:
            file_size = os.path.getsize(filepath)
            if file_size > 100 * 1024 * 1024:
                logger.info(f"Skipping scan for large file ({file_size / 1024 / 1024:.0f} MB): {filename}")
                return
        except OSError:
            return

        # Scan with ClamAV if available
        if self.use_clamav:
            logger.info(f"Scanning {filename} with ClamAV...")
            result = scan_file_clamav(filepath)

            if result["error"]:
                logger.warning(f"Scan error for {filename}: {result['error']}")
            elif not result["clean"]:
                logger.critical(
                    f"🚨 MALWARE DETECTED in {filename}: {result['threat']}"
                )
                self._quarantine_file(filepath, result["threat"])
                if self.on_threat:
                    self.on_threat(filepath, result["threat"])

    def _quarantine_file(self, filepath: str, threat_name: str):
        """Move a malicious file to quarantine."""
        try:
            filename = os.path.basename(filepath)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            quarantine_name = f"{timestamp}_{threat_name}_{filename}"
            quarantine_dest = os.path.join(self.quarantine_path, quarantine_name)

            shutil.move(filepath, quarantine_dest)
            logger.info(f"File quarantined: {filepath} → {quarantine_dest}")
        except Exception as e:
            logger.error(f"Failed to quarantine {filepath}: {e}")
            # Try to at least delete it
            try:
                os.remove(filepath)
                logger.info(f"Malicious file deleted: {filepath}")
            except Exception as e2:
                logger.error(f"Failed to delete malicious file: {e2}")


class DownloadGuard:
    """
    Main download protection manager.
    Watches download directories and scans new files.
    """

    def __init__(
        self,
        watch_paths: Optional[List[str]] = None,
        quarantine_path: Optional[str] = None,
        on_threat: Optional[Callable] = None,
        on_dangerous_ext: Optional[Callable] = None,
    ):
        self.watch_paths = watch_paths or _get_download_paths()
        self.quarantine_path = quarantine_path or _get_quarantine_path()
        self.on_threat = on_threat
        self.on_dangerous_ext = on_dangerous_ext
        self._observer: Optional[Observer] = None

    def start(self):
        """Start watching download directories."""
        handler = DownloadHandler(
            quarantine_path=self.quarantine_path,
            on_threat=self.on_threat,
            on_dangerous_ext=self.on_dangerous_ext,
        )

        self._observer = Observer()
        for path in self.watch_paths:
            if os.path.exists(path):
                self._observer.schedule(handler, path, recursive=False)
                logger.info(f"Watching downloads in: {path}")
            else:
                logger.warning(f"Watch path does not exist: {path}")

        self._observer.start()
        logger.info("Download guard started")

    def stop(self):
        """Stop watching."""
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            logger.info("Download guard stopped")

    @property
    def is_running(self) -> bool:
        return self._observer is not None and self._observer.is_alive()


if __name__ == "__main__":
    # Quick test
    logging.basicConfig(level=logging.DEBUG)
    print(f"Download paths: {_get_download_paths()}")
    print(f"Quarantine path: {_get_quarantine_path()}")
    print(f"ClamAV available: {_is_clamav_available()}")

    guard = DownloadGuard(
        on_threat=lambda fp, t: print(f"THREAT: {t} in {fp}"),
        on_dangerous_ext=lambda fp, e: print(f"DANGEROUS: {e} file: {fp}"),
    )
    guard.start()
    print("Watching... press Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        guard.stop()
