"""
Guardian Shield — Block Screen Launcher
Opens the NUH UH fullscreen screen in a browser when the agent is killed.
Serves the HTML via a tiny local HTTP server so browsers can load it.
Works on Windows (Chrome/Edge) and Linux/Pi (Chromium + DISPLAY=:99).
"""

import http.server
import logging
import os
import platform
import socket
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("guardian.block_screen")

AGENT_DIR = Path(__file__).parent


# ── Find a free port ─────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


# ── Tiny HTTP file server ────────────────────────────────────────────────────

class _FileHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args): pass


def _serve_dir(directory: Path, port: int, duration: int):
    """Serve a directory over HTTP for `duration` seconds, then stop."""
    os.chdir(str(directory))
    server = http.server.HTTPServer(("localhost", port), _FileHandler)
    server.timeout = 1
    deadline = time.time() + duration
    while time.time() < deadline:
        server.handle_request()
    server.server_close()
    os.chdir(str(AGENT_DIR))  # reset cwd


# ── Browser launcher ─────────────────────────────────────────────────────────

def _open_browser(url: str):
    """Open URL in fullscreen/kiosk mode on the current platform."""
    sys = platform.system()

    if sys == "Windows":
        # Try Chrome kiosk → Edge fullscreen → default browser
        for cmd in [
            f'start "" /B "chrome" --kiosk --disable-infobars --no-first-run "{url}"',
            f'start "" /B chrome  --start-fullscreen --app="{url}"',
            f'start "" /B "msedge" --kiosk "{url}"',
            f'start "" "{url}"',
        ]:
            try:
                ret = subprocess.Popen(cmd, shell=True)
                if ret:
                    return
            except Exception:
                continue

    else:  # Linux / Raspberry Pi
        env = os.environ.copy()
        env.setdefault("DISPLAY", ":99")
        for cmd in [
            ["chromium-browser", "--kiosk", "--no-sandbox",
             "--disable-features=TranslateUI", url],
            ["chromium", "--kiosk", "--no-sandbox", url],
            ["firefox", "--fullscreen", url],
            ["xdg-open", url],
        ]:
            try:
                subprocess.Popen(
                    cmd, env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except FileNotFoundError:
                continue


# ── Public API ────────────────────────────────────────────────────────────────

def show_nuh_uh(duration: int = 12):
    """
    Show the fullscreen NUH UH screen for `duration` seconds.
    Non-blocking — runs server and browser launch in background threads.
    """
    port = _free_port()

    # Serve the agent directory (so HTML can load relative assets)
    server_thread = threading.Thread(
        target=_serve_dir,
        args=(AGENT_DIR, port, duration + 5),
        daemon=True,
    )
    server_thread.start()
    time.sleep(0.3)  # let server bind

    url = f"http://localhost:{port}/nuh_uh.html"
    browser_thread = threading.Thread(
        target=_open_browser, args=(url,), daemon=True
    )
    browser_thread.start()
    logger.info(f"NUH UH screen opened: {url}")


def show_warning_page(host: str, count: int):
    """
    Show the animated warning page for a blocked site attempt.
    (Usually called from block_page_server — this is for manual triggers.)
    """
    port = _free_port()

    server_thread = threading.Thread(
        target=_serve_dir,
        args=(AGENT_DIR, port, 30),
        daemon=True,
    )
    server_thread.start()
    time.sleep(0.3)

    url = f"http://localhost:{port}/warning_page.html?host={host}&count={count}"
    threading.Thread(target=_open_browser, args=(url,), daemon=True).start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    print("Showing NUH UH test screen for 10 seconds...")
    show_nuh_uh(duration=10)
    time.sleep(12)
    print("Done.")
