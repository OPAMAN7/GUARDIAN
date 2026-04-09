"""
Guardian Shield — Watchdog Module
A secondary guardian process that ensures the main agent stays alive.

NEW in this version:
  - Plays a LOUD buzzer alarm (level 3) when agent kill is detected
  - Shows the full-screen NUH UH animation in a browser
  - Tracks kill attempt count and logs each incident
  - Restarts the agent within ~2 seconds

Works on both Windows (Task Manager kill) and Linux/Pi (kill command).
"""

import os
import sys
import time
import signal
import logging
import platform
import subprocess
import threading
from pathlib import Path
from typing import Optional

import psutil

logger = logging.getLogger("guardian.watchdog")

# How often to check if the main agent is alive
CHECK_INTERVAL = 3  # seconds

# How long to wait before restarting after detecting agent is dead
RESTART_DELAY = 1  # seconds

# PID file to track the main agent process
PID_DIR = Path.home() / ".guardian-shield"
AGENT_PID_FILE       = PID_DIR / "agent.pid"
WATCHDOG_PID_FILE    = PID_DIR / "watchdog.pid"
KILL_COUNT_FILE      = PID_DIR / "kill_attempts.txt"

# Flag: has the agent been seen running at least once?
_agent_was_alive = False


def _ensure_pid_dir():
    """Create the PID directory if it doesn't exist."""
    PID_DIR.mkdir(parents=True, exist_ok=True)


def write_pid(pid_file: Path, pid: int):
    """Write a PID to a file."""
    _ensure_pid_dir()
    pid_file.write_text(str(pid), encoding="utf-8")


def read_pid(pid_file: Path) -> Optional[int]:
    """Read a PID from a file. Returns None if file doesn't exist or is invalid."""
    try:
        if pid_file.exists():
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            return pid
    except (ValueError, OSError):
        pass
    return None


def is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def get_agent_command() -> list:
    """
    Get the command to start the agent.
    Returns the Python executable and the main.py script path.
    """
    agent_dir = Path(__file__).parent
    main_script = agent_dir / "main.py"
    python_exe = sys.executable
    return [python_exe, str(main_script), "--no-watchdog"]


class Watchdog:
    """
    Monitors the main agent process and restarts it if killed.
    The watchdog itself runs as a separate process (or service).
    """

    def __init__(self):
        self._running = False
        self._agent_process: Optional[subprocess.Popen] = None

    def start(self):
        """Start the watchdog monitoring loop."""
        self._running = True
        write_pid(WATCHDOG_PID_FILE, os.getpid())
        logger.info(f"Watchdog started (PID: {os.getpid()})")

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        while self._running:
            try:
                self._check_and_restart()
                time.sleep(CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"Watchdog error: {e}")
                time.sleep(CHECK_INTERVAL)

    def _handle_signal(self, signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info(f"Watchdog received signal {signum}, shutting down...")
        self._running = False

    def _check_and_restart(self):
        """Check if the agent is running. If not, fire alarms and restart it."""
        global _agent_was_alive
        agent_pid = read_pid(AGENT_PID_FILE)

        if agent_pid and is_process_alive(agent_pid):
            _agent_was_alive = True   # confirmed running at least once
            return

        # ── Agent appears dead ──────────────────────────────────────────
        if agent_pid:
            logger.warning(f"Agent process {agent_pid} is NOT running!")
        else:
            logger.warning("No agent PID file found — waiting for first start")
            return  # Don't alarm on very first boot (no PID yet)

        # Wait a moment, then double-check
        time.sleep(RESTART_DELAY)
        agent_pid = read_pid(AGENT_PID_FILE)
        if agent_pid and is_process_alive(agent_pid):
            return

        # Only trigger alarms if the agent had been alive before
        if _agent_was_alive:
            self._trigger_kill_alarms()

        self._restart_agent()

    def _trigger_kill_alarms(self):
        """Play buzzer + show NUH UH screen when agent kill is detected."""
        # 1. Increment kill attempt counter
        try:
            count = int(KILL_COUNT_FILE.read_text().strip()) + 1 if KILL_COUNT_FILE.exists() else 1
            KILL_COUNT_FILE.write_text(str(count))
        except Exception:
            count = 1
        logger.warning(f"KILL ATTEMPT #{count} DETECTED — triggering alarms!")

        # 2. Play loud alarm buzzer (non-blocking)
        try:
            from buzzer import play_warning
            play_warning(3)  # level 3 = ALARM
        except Exception as e:
            logger.error(f"Buzzer failed: {e}")

        # 3. Show NUH UH fullscreen screen in browser
        try:
            from block_screen import show_nuh_uh
            show_nuh_uh(duration=10)
        except Exception as e:
            logger.error(f"NUH UH screen failed: {e}")

    def _restart_agent(self):
        """Start/restart the agent process."""
        cmd = get_agent_command()
        logger.info(f"Starting agent: {' '.join(cmd)}")

        try:
            # Start the agent as a detached subprocess
            if platform.system() == "Windows":
                # CREATE_NO_WINDOW flag to hide console
                CREATE_NO_WINDOW = 0x08000000
                self._agent_process = subprocess.Popen(
                    cmd,
                    creationflags=CREATE_NO_WINDOW,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                self._agent_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setsid,
                )

            logger.info(f"Agent restarted with PID: {self._agent_process.pid}")
            write_pid(AGENT_PID_FILE, self._agent_process.pid)

        except Exception as e:
            logger.error(f"Failed to restart agent: {e}")

    def stop(self):
        """Stop the watchdog and optionally the agent."""
        self._running = False
        logger.info("Watchdog stopped")


def start_watchdog_subprocess():
    """
    Start the watchdog as a separate detached process.
    Called by the main agent to ensure the watchdog is running.
    """
    # Check if watchdog is already running
    watchdog_pid = read_pid(WATCHDOG_PID_FILE)
    if watchdog_pid and is_process_alive(watchdog_pid):
        logger.info(f"Watchdog already running (PID: {watchdog_pid})")
        return watchdog_pid

    # Start the watchdog
    cmd = [sys.executable, __file__]
    logger.info(f"Starting watchdog subprocess: {' '.join(cmd)}")

    if platform.system() == "Windows":
        CREATE_NO_WINDOW = 0x08000000
        proc = subprocess.Popen(
            cmd,
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )

    logger.info(f"Watchdog subprocess started (PID: {proc.pid})")
    return proc.pid


if __name__ == "__main__":
    # Run as standalone watchdog process
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [WATCHDOG] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                str(PID_DIR / "watchdog.log"),
                encoding="utf-8",
            ),
        ],
    )
    _ensure_pid_dir()
    watchdog = Watchdog()
    watchdog.start()
