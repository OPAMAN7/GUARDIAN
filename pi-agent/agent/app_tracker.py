"""
Guardian Shield — Application Tracker & Controller
Monitors active foreground applications, lists running user processes,
discovers installed applications, enforces application blocklists,
manages application usage timers, and displays parent messages to the child.
"""

import os
import sys
import time
import json
import logging
import platform
import threading
import subprocess
import shutil
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime

import psutil

logger = logging.getLogger("guardian.app_tracker")

# Noise filter: common background system processes to exclude from user apps view
SYSTEM_PROCESS_NAMES = {
    # Windows core & background daemons
    "system idle process", "system", "registry", "smss.exe", "csrss.exe",
    "wininit.exe", "services.exe", "lsass.exe", "svchost.exe", "fontdrvhost.exe",
    "dwm.exe", "memory compression", "spoolsv.exe", "sihost.exe", "taskhostw.exe",
    "ctfmon.exe", "conhost.exe", "dllhost.exe", "wlanext.exe", "searchindexer.exe",
    "securityhealthservice.exe", "sgrmbroker.exe", "smartscreen.exe",
    "runtimebroker.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "textinputhost.exe", "applicationframehost.exe", "searchhost.exe",
    "audiodg.exe", "wmiprvse.exe", "msmpeng.exe", "nissrv.exe",
    "dashost.exe", "devicecensus.exe", "compattelrunner.exe",
    # Linux core & background daemons
    "systemd", "kthreadd", "ksoftirqd", "kworker", "rcu_sched", "migration",
    "watchdog", "systemd-journald", "systemd-udevd", "systemd-timesyncd",
    "systemd-resolved", "systemd-logind", "dbus-daemon", "avahi-daemon",
    "rsyslogd", "cron", "polkitd", "accounts-daemon", "wpa_supplicant",
    "dhcpcd", "sshd", "lightdm", "pulseaudio", "pipewire", "pipewire-pulse",
    "xorg", "wayfire", "wf-panel-pi", "labwc", "openbox", "ssh", "agetty"
}

# BUG-13 FIX: processes the blocklist may NEVER terminate, no matter what rule
# matches. The Pi agent runs as User=root, so an over-broad rule like "python" or
# "system" would otherwise kill the agent itself, the desktop session, or init.
NEVER_KILL_PROCESS_NAMES = {
    # Our own runtime
    "python", "python3", "python3.9", "python3.10", "python3.11", "python3.12",
    "python3.13", "python.exe", "pythonw.exe", "py.exe",
    # Init / service management
    "systemd", "init", "systemd-journald", "systemd-udevd", "systemd-logind",
    "systemd-resolved", "systemd-timesyncd", "dbus-daemon", "polkitd",
    "services.exe", "wininit.exe", "smss.exe", "csrss.exe", "lsass.exe",
    "svchost.exe", "winlogon.exe",
    # Remote access — killing these locks us out of the device
    "sshd", "ssh", "sshd-session",
    # Desktop session — killing these blanks the child's screen, not the app
    "xorg", "wayland", "labwc", "wayfire", "wf-panel-pi", "openbox", "lightdm",
    "lxsession", "pcmanfm", "explorer.exe", "dwm.exe", "winlogon",
    # Networking
    "wpa_supplicant", "dhcpcd", "networkmanager", "nm-applet",
    # Our own helper processes
    "guardian_watchdog.py", "guardian-agent",
}

# A rule shorter than this must match the executable base name exactly rather than
# as a substring. Stops "ro" or "app" from matching half the process table.
MIN_SUBSTRING_RULE_LEN = 5


class AppTracker:
    """
    Core application tracking, blocking, timer enforcement and message delivery.
    """

    def __init__(
        self,
        on_alert: Optional[Callable[[str, str, dict], None]] = None,
        on_timer_status: Optional[Callable[[dict], None]] = None,
        enable_buzzer: bool = True,
    ):
        self.on_alert = on_alert
        self.on_timer_status = on_timer_status
        self.enable_buzzer = enable_buzzer
        self._running = False
        # BUG-10 FIX: must be reentrant. set_blocked_apps() holds this lock and
        # calls _kill_blocked_processes() -> _is_blocked(), which re-acquires it.
        # With a plain Lock that deadlocked silently and wedged every thread that
        # later touched the tracker (timers, telemetry, app sync).
        self._lock = threading.RLock()

        # Blocked process names or lowercase substrings (e.g. 'discord.exe', 'roblox')
        self._blocked_apps: set[str] = set()

        # Active timers: dict mapping timer_id -> timer dict
        # { id, app_name, process_name, duration_sec, start_time, expires_at, warned }
        self._timers: Dict[str, dict] = {}

        # Tracking state
        self._last_active_app: Optional[dict] = None
        self._active_app_start_time: float = time.time()
        self._installed_apps_cache: List[dict] = []
        self._last_installed_scan_time: float = 0.0

        # Background thread handles
        self._monitor_thread: Optional[threading.Thread] = None

    def start(self):
        """Start the background app monitoring loop."""
        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._background_monitor_loop,
            daemon=True,
            name="app-tracker-monitor",
        )
        self._monitor_thread.start()
        logger.info("Application Tracker started")

    def stop(self):
        """Stop background tracking."""
        self._running = False
        logger.info("Application Tracker stopped")

    # ─── Blocklist Management ───────────────────────────────────────────────

    def set_blocked_apps(self, apps: List[str]):
        """Update the list of blocked application process names or keywords."""
        with self._lock:
            self._blocked_apps = {
                app.strip().lower()
                for app in apps
                if app and isinstance(app, str) and app.strip()
            }
            logger.info(f"Updated blocked apps list: {len(self._blocked_apps)} apps")

        # BUG-10 FIX: run the enforcement sweep OUTSIDE the lock. The scan walks
        # every process and can take seconds; holding the lock through it would
        # stall telemetry, timers and the app-sync loop even with an RLock.
        self._kill_blocked_processes()

    def get_blocked_apps(self) -> List[str]:
        with self._lock:
            return sorted(list(self._blocked_apps))

    def _is_protected_process(self, proc: psutil.Process, p_name: str) -> bool:
        """
        BUG-13 FIX: never terminate our own process tree, PID 1, or anything on the
        NEVER_KILL allowlist. The agent runs as root on the Pi, so an over-broad
        blocklist rule would otherwise take out the agent, the desktop or sshd.
        """
        try:
            pid = proc.pid
        except Exception:
            return True

        if pid <= 1:
            return True

        own_pid = os.getpid()
        if pid == own_pid:
            return True
        try:
            if pid == os.getppid():
                return True
        except Exception:
            pass

        base = (p_name or "").lower().strip()
        if base in NEVER_KILL_PROCESS_NAMES:
            return True
        if Path(base).stem in NEVER_KILL_PROCESS_NAMES:
            return True

        # Anything in our own ancestry, and any process running our own script.
        try:
            if own_pid in {p.pid for p in proc.children(recursive=True)}:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return True
        except Exception:
            pass

        try:
            cmdline = " ".join(proc.cmdline()).lower()
            if "guardian" in cmdline and (".py" in cmdline or "guardian-agent" in cmdline):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return True
        except Exception:
            pass

        return False

    def _is_blocked(self, process_name: str, window_title: str = "") -> bool:
        """Check if process name or window title matches any blocked rule."""
        p_name = (process_name or "").lower().strip()
        w_title = (window_title or "").lower().strip()
        p_stem = Path(p_name).stem if p_name else ""

        with self._lock:
            rules = tuple(self._blocked_apps)

        for rule in rules:
            if not rule:
                continue
            rule_stem = Path(rule).stem
            # 1. Exact match on the full name or the base name, either direction.
            if rule == p_name or rule_stem == p_stem:
                return True
            # 2. BUG-13 FIX: only long, specific rules are allowed to match as a
            #    substring. Short rules ("py", "app", "ro") must match exactly,
            #    which is handled above.
            if len(rule) >= MIN_SUBSTRING_RULE_LEN and rule in p_name:
                return True
            # 3. Window-title keyword match, for launchers whose process name
            #    differs from the game (e.g. 'roblox', 'minecraft').
            if len(rule) >= MIN_SUBSTRING_RULE_LEN and w_title and rule in w_title:
                return True
        return False

    def _kill_blocked_processes(self):
        """Scan running processes and terminate any blocked apps."""
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                info = proc.info
                p_name = info.get("name") or ""
                if not p_name:
                    continue
                if not self._is_blocked(p_name):
                    continue
                if self._is_protected_process(proc, p_name):
                    logger.warning(
                        f"Blocklist rule matched protected process '{p_name}' "
                        f"(PID {info.get('pid')}) — refusing to kill it. "
                        f"Make the rule more specific."
                    )
                    continue
                pid = info.get("pid")
                logger.warning(f"🚫 Killing blocked application: {p_name} (PID: {pid})")
                self._terminate_process_tree(proc)
                self._notify_app_blocked(p_name, pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception as e:
                logger.debug(f"Error checking process: {e}")

    def _terminate_process_tree(self, proc: psutil.Process):
        """Cleanly terminate process and all child sub-processes."""
        try:
            children = proc.children(recursive=True)
            for child in children:
                try:
                    child.terminate()
                except Exception:
                    pass
            proc.terminate()
            gone, alive = psutil.wait_procs([proc] + children, timeout=1.0)
            for p in alive:
                try:
                    p.kill()
                except Exception:
                    pass
        except Exception as e:
            try:
                proc.kill()
            except Exception:
                pass
            logger.debug(f"Process termination note: {e}")

    def _notify_app_blocked(self, app_name: str, pid: Optional[int]):
        """Alert parent dashboard and notify child when an app is blocked."""
        msg = f"Blocked app closed: {app_name}"
        if self.on_alert:
            try:
                self.on_alert(
                    "app_blocked",
                    msg,
                    {"app_name": app_name, "pid": pid, "time": time.time()},
                )
            except Exception as e:
                logger.error(f"Alert callback error: {e}")

        # Show non-blocking popup to child
        self.show_child_message(
            title="🚫 App Restricted",
            message=f"'{app_name}' has been blocked by your parent.",
            urgency="warning",
            duration=6,
        )

    # ─── Close App Instantly (Anonymous Kill) ───────────────────────────────

    def close_app(self, pid: Optional[int] = None, process_name: Optional[str] = None) -> bool:
        """
        Close/terminate an application by PID or process name anonymously.
        Returns True if at least one matching process was terminated.
        """
        killed = False
        target_name = (process_name or "").lower().strip()

        # 1. Target by PID
        if pid and pid > 0:
            try:
                p = psutil.Process(pid)
                p_name = p.name()
                # BUG-13 FIX: same guard as the blocklist sweep — a mistyped PID
                # must not be able to take down the agent, sshd or the session.
                if self._is_protected_process(p, p_name):
                    logger.warning(
                        f"Refusing to close protected process '{p_name}' (PID {pid})"
                    )
                    return False
                logger.info(f"Closing app by PID {pid} ({p_name})")
                self._terminate_process_tree(p)
                killed = True
                if self.on_alert:
                    self.on_alert("app_closed", f"Closed application: {p_name}", {"pid": pid, "process_name": p_name})
                return True
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                logger.warning(f"Could not kill PID {pid}: {e}")

        # 2. Target by Process Name
        if target_name:
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    info = p.info
                    name = (info.get("name") or "").lower()
                    if name == target_name or (name.endswith(".exe") and name[:-4] == target_name):
                        if self._is_protected_process(p, name):
                            logger.warning(
                                f"Refusing to close protected process '{name}' "
                                f"(PID {info.get('pid')})"
                            )
                            continue
                        logger.info(f"Closing app by name: {name} (PID: {info.get('pid')})")
                        self._terminate_process_tree(p)
                        killed = True
                except Exception:
                    continue

        if killed and self.on_alert:
            self.on_alert("app_closed", f"Closed application: {target_name}", {"process_name": target_name})

        return killed

    # ─── App Timer Management ───────────────────────────────────────────────

    def set_app_timer(
        self,
        app_name: str,
        process_name: str,
        duration_seconds: int,
        timer_id: Optional[str] = None,
    ) -> dict:
        """
        Set a timer to automatically close an application after duration_seconds.
        """
        duration = max(10, int(duration_seconds))
        t_id = timer_id or f"timer_{int(time.time())}_{process_name}"
        now = time.time()
        expires_at = now + duration

        timer_record = {
            "id": t_id,
            "app_name": app_name or process_name,
            "process_name": process_name.lower().strip(),
            "duration_seconds": duration,
            "start_time": now,
            "expires_at": expires_at,
            "warned_5m": False,
            "warned_1m": False,
            "status": "active",
        }

        with self._lock:
            # Replace existing timer for same process if any
            for existing_id, t in list(self._timers.items()):
                if t.get("process_name") == timer_record["process_name"]:
                    del self._timers[existing_id]
            self._timers[t_id] = timer_record

        logger.info(f"⏳ Set app timer for '{app_name}' ({process_name}): {duration}s")

        # Notify child with initial toast
        minutes = round(duration / 60)
        time_text = f"{minutes} minute{'s' if minutes != 1 else ''}" if duration >= 60 else f"{duration} seconds"
        self.show_child_message(
            title="⏱️ Time Limit Set",
            message=f"You have {time_text} of screen time remaining for '{app_name}'.",
            urgency="info",
            duration=6,
        )

        if self.on_timer_status:
            self.on_timer_status(timer_record)

        return timer_record

    def cancel_app_timer(self, timer_id_or_process: str) -> bool:
        """Cancel a running app timer."""
        cancelled = False
        target = (timer_id_or_process or "").lower().strip()

        with self._lock:
            for t_id, timer in list(self._timers.items()):
                if t_id == timer_id_or_process or timer.get("process_name") == target:
                    timer["status"] = "cancelled"
                    if self.on_timer_status:
                        self.on_timer_status(timer)
                    del self._timers[t_id]
                    cancelled = True
                    logger.info(f"Cancelled app timer for {target}")

        return cancelled

    def get_active_timers(self) -> List[dict]:
        """Return list of active timers with real-time remaining seconds."""
        now = time.time()
        active = []
        with self._lock:
            for t in self._timers.values():
                rem = max(0, int(t["expires_at"] - now))
                copy_t = dict(t)
                copy_t["remaining_seconds"] = rem
                active.append(copy_t)
        return active

    def _check_timers(self):
        """Check active timers and handle warnings/closing when expired."""
        now = time.time()
        expired_timers = []

        with self._lock:
            for t_id, timer in list(self._timers.items()):
                remaining = timer["expires_at"] - now
                app_name = timer.get("app_name") or timer.get("process_name")

                # 5 minute warning
                if 240 <= remaining <= 300 and not timer.get("warned_5m"):
                    timer["warned_5m"] = True
                    self.show_child_message(
                        title="⏳ 5 Minutes Remaining",
                        message=f"'{app_name}' will close in 5 minutes. Please save your work!",
                        urgency="warning",
                        duration=8,
                    )

                # 1 minute warning
                if 30 <= remaining <= 60 and not timer.get("warned_1m"):
                    timer["warned_1m"] = True
                    self.show_child_message(
                        title="⚠️ 1 Minute Remaining",
                        message=f"'{app_name}' will close in 1 minute. Wrapping up now!",
                        urgency="warning",
                        duration=8,
                    )

                # Expired
                if remaining <= 0:
                    timer["status"] = "expired"
                    expired_timers.append(dict(timer))
                    del self._timers[t_id]

        for expired in expired_timers:
            proc_name = expired.get("process_name")
            app_name = expired.get("app_name") or proc_name
            logger.info(f"⏳ Timer expired for '{app_name}' — terminating process...")
            self.close_app(process_name=proc_name)

            self.show_child_message(
                title="⏰ Time's Up!",
                message=f"Time limit reached for '{app_name}'. The app has been closed.",
                urgency="urgent",
                duration=10,
            )

            if self.on_alert:
                self.on_alert(
                    "app_timer_expired",
                    f"Time limit reached: '{app_name}' closed automatically.",
                    {"app_name": app_name, "process_name": proc_name},
                )
            if self.on_timer_status:
                self.on_timer_status(expired)

    # ─── Live Foreground & Running Apps ─────────────────────────────────────

    def get_foreground_app(self) -> dict:
        """
        Get the currently active / focused application and window title.
        Works across Windows and Linux.
        """
        active = {
            "app_name": "Desktop / System",
            "process_name": "explorer.exe" if platform.system() == "Windows" else "desktop",
            "window_title": "",
            "pid": 0,
            "cpu_percent": 0.0,
            "memory_mb": 0.0,
            "duration_sec": 0,
            "platform": platform.system().lower(),
        }

        if platform.system() == "Windows":
            active = self._get_windows_foreground_app()
        else:
            active = self._get_linux_foreground_app()

        now = time.time()
        # Track continuous duration on the active application
        if (
            self._last_active_app
            and self._last_active_app.get("process_name") == active.get("process_name")
            and (now - self._active_app_start_time) < 86400
        ):
            active["duration_sec"] = int(now - self._active_app_start_time)
        else:
            self._active_app_start_time = now
            self._last_active_app = active
            active["duration_sec"] = 0

        return active

    def get_foreground_snapshot(self) -> dict:
        """Read the focused window WITHOUT touching any shared duration state.

        ``get_foreground_app()`` mutates ``self._last_active_app`` and
        ``self._active_app_start_time`` outside the lock, and two loops
        (``_collect_telemetry`` and ``_app_sync_loop``) already call it. Adding a
        third caller would corrupt ``duration_sec`` for the other two: whichever
        loop polled last resets the start time, so every reported duration
        collapses towards zero.

        The usage collector needs the raw fact ("what is focused right now") and
        does its own session timing, so it uses this side-effect-free read
        instead. ``duration_sec`` is intentionally always 0 here — the caller
        owns the clock.

        Added for the academic-progress collector (F-18). Nothing existing calls
        it, so behaviour of the shipped Apps page is unchanged.
        """
        if platform.system() == "Windows":
            snap = self._get_windows_foreground_app()
        else:
            snap = self._get_linux_foreground_app()
        if not isinstance(snap, dict):
            return {}
        snap = dict(snap)
        snap["duration_sec"] = 0
        snap["sampled_at"] = time.time()
        return snap

    def _get_windows_foreground_app(self) -> dict:
        import ctypes
        import ctypes.wintypes

        data = {
            "app_name": "Desktop / System",
            "process_name": "explorer.exe",
            "window_title": "",
            "pid": 0,
            "cpu_percent": 0.0,
            "memory_mb": 0.0,
        }

        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return data

            length = user32.GetWindowTextLengthW(hwnd)
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value.strip()

            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            if pid.value > 0:
                p = psutil.Process(pid.value)
                p_name = p.name()
                mem = round(p.memory_info().rss / (1024 * 1024), 1)
                cpu = round(p.cpu_percent(interval=None), 1)

                clean_app_name = self._clean_app_name(p_name, title)
                data.update({
                    "app_name": clean_app_name,
                    "process_name": p_name,
                    "window_title": title,
                    "pid": pid.value,
                    "cpu_percent": cpu,
                    "memory_mb": mem,
                })
        except Exception as e:
            logger.debug(f"Windows active window read error: {e}")

        return data

    def _get_linux_foreground_app(self) -> dict:
        data = {
            "app_name": "Desktop / Workspace",
            "process_name": "desktop",
            "window_title": "",
            "pid": 0,
            "cpu_percent": 0.0,
            "memory_mb": 0.0,
        }

        env = os.environ.copy()
        if not env.get("DISPLAY") and os.path.exists("/tmp/.X11-unix/X0"):
            env["DISPLAY"] = ":0"
        if not env.get("XAUTHORITY") and os.path.exists("/home/pi/.Xauthority"):
            env["XAUTHORITY"] = "/home/pi/.Xauthority"
        if not env.get("XDG_RUNTIME_DIR") and os.path.exists("/run/user/1000"):
            env["XDG_RUNTIME_DIR"] = "/run/user/1000"

        # 1. Try xdotool if available in X11/XWayland
        try:
            xdotool = shutil.which("xdotool")
            if xdotool:
                out = subprocess.check_output([xdotool, "getactivewindow", "getwindowname"], env=env, timeout=0.8)
                title = out.decode("utf-8", errors="ignore").strip()
                pid_out = subprocess.check_output([xdotool, "getactivewindow", "getwindowpid"], env=env, timeout=0.8)
                pid = int(pid_out.decode("utf-8", errors="ignore").strip())

                if pid > 0:
                    p = psutil.Process(pid)
                    p_name = p.name()
                    mem = round(p.memory_info().rss / (1024 * 1024), 1)
                    clean_app_name = self._clean_app_name(p_name, title)
                    data.update({
                        "app_name": clean_app_name,
                        "process_name": p_name,
                        "window_title": title,
                        "pid": pid,
                        "memory_mb": mem,
                    })
                    return data
        except Exception:
            pass

        # 2. Try xprop as fallback
        try:
            xprop = shutil.which("xprop")
            if xprop:
                root_out = subprocess.check_output([xprop, "-root", "_NET_ACTIVE_WINDOW"], env=env, timeout=0.8).decode()
                win_id = root_out.split()[-1]
                if win_id and win_id not in ("0x0", "0"):
                    props = subprocess.check_output(
                        [xprop, "-id", win_id, "_NET_WM_PID", "WM_NAME", "WM_CLASS"],
                        env=env,
                        timeout=0.8
                    ).decode("utf-8", errors="ignore")
                    pid = 0
                    title = ""
                    wm_class = ""
                    for line in props.splitlines():
                        if "_NET_WM_PID" in line:
                            try:
                                pid = int(line.split("=")[-1].strip())
                            except Exception:
                                pass
                        elif "WM_NAME" in line and not title:
                            title = line.split("=", 1)[-1].strip().strip('"')
                        elif "WM_CLASS" in line and not wm_class:
                            wm_class = line.split("=", 1)[-1].strip().strip('"')

                    if pid > 0:
                        p = psutil.Process(pid)
                        p_name = p.name()
                        mem = round(p.memory_info().rss / (1024 * 1024), 1)
                        clean_app_name = self._clean_app_name(p_name, title or wm_class)
                        data.update({
                            "app_name": clean_app_name,
                            "process_name": p_name,
                            "window_title": title or wm_class,
                            "pid": pid,
                            "memory_mb": mem,
                        })
                        return data
        except Exception:
            pass

        # 3. Fallback: inspect top interactive user GUI/terminal process
        try:
            candidates = []
            for proc in psutil.process_iter(["pid", "name", "memory_info", "username"]):
                try:
                    info = proc.info
                    name = (info.get("name") or "").lower()
                    if not name or name in SYSTEM_PROCESS_NAMES or name.startswith("systemd") or name.startswith("kworker"):
                        continue
                    mem = round((info["memory_info"].rss or 0) / (1024 * 1024), 1)
                    if mem > 15.0:
                        candidates.append((info.get("pid"), info.get("name"), mem))
                except Exception:
                    continue

            if candidates:
                candidates.sort(key=lambda c: c[2], reverse=True)
                top_pid, top_name, top_mem = candidates[0]
                data.update({
                    "app_name": self._clean_app_name(top_name),
                    "process_name": top_name,
                    "window_title": "Running Application",
                    "pid": top_pid,
                    "memory_mb": top_mem,
                })
        except Exception:
            pass

        return data

    def get_running_apps(self, limit: int = 40) -> List[dict]:
        """
        List currently running interactive user applications.
        Filters out kernel threads and core background services.
        """
        apps_dict = {}
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "create_time", "username"]):
            try:
                info = proc.info
                name = info.get("name") or ""
                if not name:
                    continue

                low_name = name.lower()
                if low_name in SYSTEM_PROCESS_NAMES or low_name.startswith("systemd") or low_name.startswith("kworker"):
                    continue

                mem_mb = round((info["memory_info"].rss or 0) / (1024 * 1024), 1) if info.get("memory_info") else 0.0
                if mem_mb < 8.0:
                    continue  # Ignore tiny micro-processes

                pid = info.get("pid")
                cpu = round(float(info.get("cpu_percent") or 0.0), 1)
                create_time = info.get("create_time") or time.time()
                uptime_sec = int(time.time() - create_time)

                clean_name = self._clean_app_name(name)
                is_blocked = self._is_blocked(name)

                # Group processes by name to sum memory & pick main process
                if name not in apps_dict:
                    apps_dict[name] = {
                        "app_name": clean_name,
                        "process_name": name,
                        "pid": pid,
                        "instance_count": 1,
                        "cpu_percent": cpu,
                        "memory_mb": mem_mb,
                        "uptime_sec": uptime_sec,
                        "is_blocked": is_blocked,
                        "username": info.get("username") or "",
                    }
                else:
                    apps_dict[name]["instance_count"] += 1
                    apps_dict[name]["memory_mb"] = round(apps_dict[name]["memory_mb"] + mem_mb, 1)
                    apps_dict[name]["cpu_percent"] = round(apps_dict[name]["cpu_percent"] + cpu, 1)
                    if uptime_sec > apps_dict[name]["uptime_sec"]:
                        apps_dict[name]["uptime_sec"] = uptime_sec
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue

        # Sort by Memory Usage descending
        sorted_apps = sorted(apps_dict.values(), key=lambda x: x["memory_mb"], reverse=True)
        return sorted_apps[:limit]

    # ─── Installed Applications Discovery ───────────────────────────────────

    def get_installed_apps(self, force_refresh: bool = False) -> List[dict]:
        """
        Discover installed applications on the host device.
        Cached for 5 minutes unless force_refresh is True.
        """
        now = time.time()
        if not force_refresh and self._installed_apps_cache and (now - self._last_installed_scan_time < 300):
            return self._installed_apps_cache

        installed = {}

        if platform.system() == "Windows":
            installed = self._scan_windows_installed_apps()
        else:
            installed = self._scan_linux_installed_apps()

        # Format list
        result = []
        for app_name, details in sorted(installed.items(), key=lambda x: x[0].lower()):
            proc_name = details.get("process_name") or self._guess_process_name(app_name)
            category = details.get("category") or self._categorize_app(app_name)
            is_blocked = self._is_blocked(proc_name)

            result.append({
                "app_name": app_name,
                "process_name": proc_name,
                "path": details.get("path") or "",
                "category": category,
                "is_blocked": is_blocked,
            })

        self._installed_apps_cache = result
        self._last_installed_scan_time = now
        return result

    def _scan_windows_installed_apps(self) -> Dict[str, dict]:
        import winreg
        installed = {}

        def _scan_registry(hive, subkey):
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            skey_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, skey_name) as skey:
                                try:
                                    name = winreg.QueryValueEx(skey, "DisplayName")[0]
                                    if not name or len(name.strip()) < 2:
                                        continue
                                    name = name.strip()
                                    if name.startswith("KB") or name.startswith("Security Update"):
                                        continue

                                    install_loc = ""
                                    icon = ""
                                    try:
                                        install_loc = winreg.QueryValueEx(skey, "InstallLocation")[0]
                                    except Exception:
                                        pass
                                    try:
                                        icon = winreg.QueryValueEx(skey, "DisplayIcon")[0]
                                    except Exception:
                                        pass

                                    installed[name] = {
                                        "path": install_loc or icon or "",
                                        "process_name": Path(icon).name if icon.endswith(".exe") else "",
                                        "category": self._categorize_app(name),
                                    }
                                except Exception:
                                    pass
                        except Exception:
                            pass
            except Exception:
                pass

        _scan_registry(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")
        _scan_registry(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")
        _scan_registry(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")

        # Also scan Start Menu shortcut directory
        for base in [
            os.environ.get("APPDATA", "") + r"\Microsoft\Windows\Start Menu\Programs",
            os.environ.get("PROGRAMDATA", "") + r"\Microsoft\Windows\Start Menu\Programs",
        ]:
            if not base or not os.path.exists(base):
                continue
            for root, _, files in os.walk(base):
                for f in files:
                    if f.endswith(".lnk"):
                        app_name = f[:-4]
                        if app_name and len(app_name) > 2 and app_name not in installed:
                            installed[app_name] = {
                                "path": os.path.join(root, f),
                                "process_name": f"{app_name.lower().replace(' ', '')}.exe",
                                "category": self._categorize_app(app_name),
                            }

        return installed

    def _scan_linux_installed_apps(self) -> Dict[str, dict]:
        installed = {}
        dirs = [
            "/usr/share/applications",
            "/usr/local/share/applications",
            os.path.expanduser("~/.local/share/applications"),
            "/var/lib/snapd/desktop/applications",
        ]
        for d in dirs:
            if not os.path.exists(d):
                continue
            for item in Path(d).glob("*.desktop"):
                try:
                    lines = item.read_text(encoding="utf-8", errors="ignore").splitlines()
                    name = None
                    exec_cmd = None
                    categories = ""
                    for line in lines:
                        if line.startswith("Name=") and not name:
                            name = line.split("=", 1)[1].strip()
                        elif line.startswith("Exec=") and not exec_cmd:
                            exec_cmd = line.split("=", 1)[1].strip()
                        elif line.startswith("Categories="):
                            categories = line.split("=", 1)[1].strip()

                    if name and len(name) > 1:
                        p_name = Path(exec_cmd.split()[0]).name if exec_cmd else f"{name.lower()}.bin"
                        installed[name] = {
                            "path": str(item),
                            "process_name": p_name,
                            "category": self._categorize_app(name, categories),
                        }
                except Exception:
                    continue
        return installed

    # ─── Message Delivery to Child Screen ───────────────────────────────────

    def show_child_message(
        self,
        title: str,
        message: str,
        urgency: str = "info",
        duration: int = 10,
    ):
        """
        Display a parent message / notification banner on the child's screen.
        Runs asynchronously in a background thread without blocking.
        """
        def _dialog_worker():
            try:
                self._display_native_popup(title, message, urgency, duration)
            except Exception as e:
                logger.error(f"Failed to display child message: {e}")

        threading.Thread(target=_dialog_worker, daemon=True, name="child-msg-popup").start()

    def _display_native_popup(self, title: str, message: str, urgency: str, duration: int):
        """Build and display a styled top-most floating overlay across Windows and Linux."""
        # Ensure display environment on Linux
        if platform.system() == "Linux":
            if not os.environ.get("DISPLAY") and os.path.exists("/tmp/.X11-unix/X0"):
                os.environ["DISPLAY"] = ":0"
            if not os.environ.get("XAUTHORITY") and os.path.exists("/home/pi/.Xauthority"):
                os.environ["XAUTHORITY"] = "/home/pi/.Xauthority"
            if not os.environ.get("XDG_RUNTIME_DIR") and os.path.exists("/run/user/1000"):
                os.environ["XDG_RUNTIME_DIR"] = "/run/user/1000"

        # Play audible notification chime if enabled
        if self.enable_buzzer:
            try:
                from buzzer import play_warning
                play_warning(3 if urgency == "urgent" else 2 if urgency == "warning" else 1)
            except Exception:
                pass

        try:
            import tkinter as tk

            root = tk.Tk()
            root.title("Guardian Shield — Parent Notice")
            root.attributes("-topmost", True)
            root.overrideredirect(True)  # Frameless floating card

            # Urgency colors
            bg_card = "#0f172a"
            border_color = (
                "#ef4444" if urgency == "urgent" else "#f59e0b" if urgency == "warning" else "#3b82f6"
            )
            accent_icon = "🚨" if urgency == "urgent" else "⚠️" if urgency == "warning" else "🛡️"

            # Geometry adapted to screen resolution (supports 480x320 Pi screens up to 4K)
            screen_w = max(320, root.winfo_screenwidth())
            screen_h = max(240, root.winfo_screenheight())
            w = min(460, max(300, screen_w - 24))
            h = min(170, max(130, screen_h - 40))
            x = max(0, (screen_w - w) // 2)
            y = 20 if screen_h > 360 else 10
            root.geometry(f"{w}x{h}+{x}+{y}")
            root.configure(bg=border_color)

            # Font selection
            font_family = "Segoe UI" if platform.system() == "Windows" else "Helvetica"
            font_title = (font_family, 10, "bold")
            font_body = (font_family, 10)
            font_small = (font_family, 8)

            # Main container
            container = tk.Frame(root, bg=bg_card, bd=0)
            container.pack(fill="both", expand=True, padx=2, pady=2)

            # Header row
            header = tk.Frame(container, bg=bg_card)
            header.pack(fill="x", padx=12, pady=(10, 2))

            icon_lbl = tk.Label(header, text=accent_icon, font=(font_family, 14), bg=bg_card, fg="#ffffff")
            icon_lbl.pack(side="left", padx=(0, 6))

            title_lbl = tk.Label(
                header,
                text=f"GUARDIAN SHIELD — {title.upper()}",
                font=font_title,
                bg=bg_card,
                fg=border_color,
            )
            title_lbl.pack(side="left")

            # Message body
            msg_frame = tk.Frame(container, bg=bg_card)
            msg_frame.pack(fill="both", expand=True, padx=12, pady=2)

            msg_lbl = tk.Label(
                msg_frame,
                text=message,
                font=font_body,
                bg=bg_card,
                fg="#f1f5f9",
                wraplength=w - 30,
                justify="left",
            )
            msg_lbl.pack(anchor="w")

            # Footer / dismiss
            footer = tk.Frame(container, bg=bg_card)
            footer.pack(fill="x", padx=12, pady=(0, 8))

            time_left = [duration]
            countdown_lbl = tk.Label(
                footer,
                text=f"Auto-closing in {duration}s...",
                font=font_small,
                bg=bg_card,
                fg="#94a3b8",
            )
            countdown_lbl.pack(side="left")

            def _dismiss():
                try:
                    root.destroy()
                except Exception:
                    pass

            dismiss_btn = tk.Button(
                footer,
                text=" OK, Understood ",
                font=font_small,
                bg=border_color,
                fg="#ffffff",
                activebackground="#1e293b",
                activeforeground="#ffffff",
                bd=0,
                relief="flat",
                cursor="hand2",
                command=_dismiss,
            )
            dismiss_btn.pack(side="right")

            def _tick():
                time_left[0] -= 1
                if time_left[0] <= 0:
                    _dismiss()
                else:
                    try:
                        countdown_lbl.config(text=f"Auto-closing in {time_left[0]}s...")
                        root.after(1000, _tick)
                    except Exception:
                        pass

            root.after(1000, _tick)
            root.mainloop()

        except Exception as e:
            logger.warning(f"Tkinter popup failed, trying OS notification fallback: {e}")
            if platform.system() == "Linux":
                env = os.environ.copy()
                env.setdefault("DISPLAY", ":0")
                env.setdefault("XAUTHORITY", "/home/pi/.Xauthority")
                zenity = shutil.which("zenity")
                if zenity:
                    try:
                        flag = "--warning" if urgency in ("warning", "urgent") else "--info"
                        subprocess.Popen([
                            zenity,
                            flag,
                            f"--title=GUARDIAN: {title}",
                            f"--text={message}",
                            f"--timeout={duration}",
                        ], env=env)
                        return
                    except Exception:
                        pass
                notify_send = shutil.which("notify-send")
                if notify_send:
                    try:
                        urg_flag = "critical" if urgency == "urgent" else "normal"
                        subprocess.Popen([notify_send, "-u", urg_flag, title, message], env=env)
                    except Exception:
                        pass

    # ─── Internal Helper & Loop ──────────────────────────────────────────────

    def _background_monitor_loop(self):
        """Continuous background monitor for blocked apps and expiring timers."""
        while self._running:
            try:
                # 1. Enforce blocked applications
                if self._blocked_apps:
                    self._kill_blocked_processes()

                # 2. Check and process timers
                if self._timers:
                    self._check_timers()
            except Exception as e:
                logger.debug(f"App monitor loop tick error: {e}")

            time.sleep(1.5)

    def _clean_app_name(self, proc_name: str, window_title: str = "") -> str:
        """Convert a raw process name like msedge.exe or code.exe into a human-friendly name."""
        p = (proc_name or "").lower()
        mapping = {
            "chrome.exe": "Google Chrome",
            "msedge.exe": "Microsoft Edge",
            "firefox.exe": "Mozilla Firefox",
            "brave.exe": "Brave Browser",
            "opera.exe": "Opera Browser",
            "code.exe": "Visual Studio Code",
            "antigravity ide.exe": "Antigravity IDE",
            "discord.exe": "Discord",
            "spotify.exe": "Spotify",
            "steam.exe": "Steam",
            "steamwebhelper.exe": "Steam Client",
            "robloxplayerbeta.exe": "Roblox",
            "javaw.exe": "Minecraft / Java Game",
            "minecraft.exe": "Minecraft",
            "vlc.exe": "VLC Media Player",
            "notepad.exe": "Notepad",
            "calculator.exe": "Calculator",
            "mspaint.exe": "Paint",
            "telegram.exe": "Telegram",
            "whatsapp.exe": "WhatsApp",
            "zoom.exe": "Zoom Meetings",
            "teams.exe": "Microsoft Teams",
            "explorer.exe": "Windows Explorer / Desktop",
        }
        if p in mapping:
            return mapping[p]

        # Extract title prefix if clean
        if window_title and len(window_title) > 2 and " - " in window_title:
            parts = window_title.split(" - ")
            candidate = parts[-1].strip()
            if len(candidate) > 2 and len(candidate) < 30:
                return candidate

        base = proc_name.replace(".exe", "").replace(".bin", "").title()
        return base or "Unknown App"

    def _guess_process_name(self, app_name: str) -> str:
        clean = app_name.lower().replace(" ", "").replace("-", "")
        return f"{clean}.exe" if platform.system() == "Windows" else clean

    def _categorize_app(self, app_name: str, hint: str = "") -> str:
        text = f"{app_name} {hint}".lower()
        if any(w in text for w in ["game", "steam", "roblox", "minecraft", "epic", "play", "strike", "cyberpunk"]):
            return "Games"
        if any(w in text for w in ["discord", "telegram", "whatsapp", "teams", "zoom", "chat", "slack"]):
            return "Social & Chat"
        if any(w in text for w in ["chrome", "edge", "firefox", "brave", "opera", "browser", "safari"]):
            return "Web Browser"
        if any(w in text for w in ["code", "studio", "terminal", "git", "python", "ide", "compiler"]):
            return "Development"
        if any(w in text for w in ["spotify", "vlc", "media", "music", "video", "player", "netflix"]):
            return "Media & Streaming"
        return "General"
