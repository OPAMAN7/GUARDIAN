"""
Guardian Shield — Windows Server Watchdog
Monitors the Node.js server process on the PARENT's Windows PC.

When someone tries to kill the server via Task Manager:
  1. Plays a LOUD buzzer alarm (winsound)
  2. Opens the NUH UH fullscreen screen in Chrome/Edge
  3. Restarts the Node.js server immediately

Run this instead of `node index.js` directly.
Usage: python watchdog_server.py
"""

import http.server
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SERVER-WATCHDOG] %(levelname)s: %(message)s",
)
logger = logging.getLogger("guardian.server_watchdog")

WATCHDOG_DIR = Path(__file__).parent
REPO_DIR = Path(__file__).resolve().parents[2]
SERVER_DIR = REPO_DIR / "parent-dashboard" / "server-ts"
NUH_UH_HTML = WATCHDOG_DIR / "nuh_uh.html"
STATE_FILE = WATCHDOG_DIR / "kill_attempts.txt"

SERVER_CMD   = ["node", "index.js"]
CHECK_INTERVAL = 3    # seconds
RESTART_DELAY  = 0.8  # seconds after kill detected before restart


# ── Sound ────────────────────────────────────────────────────────────────────

def _play_alarm():
    """Play rapid alarm on Windows using winsound."""
    try:
        import winsound
        for _ in range(7):
            winsound.Beep(1600, 140)
            winsound.Beep(900, 140)
    except Exception as e:
        logger.debug(f"Sound error: {e}")


# ── NUH UH Screen ────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class _FileHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass


def _serve_file(directory: Path, port: int, duration: int):
    os.chdir(str(directory))
    srv = http.server.HTTPServer(("localhost", port), _FileHandler)
    srv.timeout = 1
    deadline = time.time() + duration
    while time.time() < deadline:
        srv.handle_request()
    srv.server_close()


def _show_nuh_uh():
    port = _free_port()
    t = threading.Thread(target=_serve_file, args=(WATCHDOG_DIR, port, 15), daemon=True)
    t.start()
    time.sleep(0.3)

    url = f"http://localhost:{port}/nuh_uh.html"
    for cmd in [
        f'start "" /B chrome  --kiosk --disable-infobars "{url}"',
        f'start "" /B chrome  --start-fullscreen --app="{url}"',
        f'start "" /B msedge  --kiosk "{url}"',
        f'start "" /B msedge  --start-fullscreen "{url}"',
        f'start "" "{url}"',
    ]:
        try:
            subprocess.Popen(cmd, shell=True)
            return
        except Exception:
            continue


# ── Kill counter ─────────────────────────────────────────────────────────────

def _inc_kill_count() -> int:
    try:
        n = int(STATE_FILE.read_text().strip()) + 1 if STATE_FILE.exists() else 1
    except Exception:
        n = 1
    try:
        STATE_FILE.write_text(str(n))
    except Exception:
        pass
    return n


# ── Main watchdog loop ───────────────────────────────────────────────────────

class ServerWatchdog:

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._was_alive = False
        self._running   = True

    def _start_server(self):
        """Launch the Node.js server as a subprocess."""
        logger.info("Starting Guardian server: node index.js")
        self._proc = subprocess.Popen(
            SERVER_CMD,
            cwd=str(SERVER_DIR),
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        logger.info(f"Server started (PID {self._proc.pid})")

    def _server_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def run(self):
        self._start_server()
        time.sleep(2)

        while self._running:
            time.sleep(CHECK_INTERVAL)

            if self._server_alive():
                self._was_alive = True
                continue

            # ── Server died ──────────────────────────────────────
            logger.warning("Server process has stopped!")

            if self._was_alive:
                count = _inc_kill_count()
                logger.warning(f"Kill/crash attempt #{count} — firing alarms!")

                # Play buzzer in background thread
                sound_thread = threading.Thread(target=_play_alarm, daemon=True)
                sound_thread.start()

                # Show NUH UH screen in background
                screen_thread = threading.Thread(target=_show_nuh_uh, daemon=True)
                screen_thread.start()

            # Wait briefly, then restart
            time.sleep(RESTART_DELAY)
            logger.info("Restarting server...")
            self._start_server()


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if platform.system() != "Windows":
        logger.warning("This watchdog is designed for Windows. On Pi, use systemd Restart=always.")

    # Copy nuh_uh.html from agent/ if it doesn't exist here
    agent_nuh = REPO_DIR / "pi-agent" / "agent" / "nuh_uh.html"
    if not NUH_UH_HTML.exists() and agent_nuh.exists():
        import shutil
        shutil.copy(agent_nuh, NUH_UH_HTML)

    logger.info("Guardian Shield — Server Watchdog Started")
    logger.info(f"Monitoring: {SERVER_DIR}")
    watcher = ServerWatchdog()
    try:
        watcher.run()
    except KeyboardInterrupt:
        logger.info("Watchdog stopped by user.")
