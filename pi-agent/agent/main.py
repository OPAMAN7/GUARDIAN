"""
Guardian Shield — Main Agent Entry Point
Orchestrates all modules: screenshot capture, website blocking,
download protection, and server communication.
"""

import os
import sys
import json
import time
import queue
import pwd
import signal
import logging
import base64
import argparse
import platform
import threading
import subprocess
import ctypes
import shutil
import psutil
from pathlib import Path
from typing import Optional
from datetime import datetime
from urllib.parse import quote

import socketio

from screenshot import ScreenshotCapture
from blocker import WebsiteBlocker, BlocklistUpdater
from download_guard import DownloadGuard
from cache import ScreenshotCache, CacheUploader
from consent import check_stealth_consent, request_stealth_consent
from guardian_watchdog import (
    write_pid, AGENT_PID_FILE, start_watchdog_subprocess, _ensure_pid_dir
)
from block_page_server import start_block_server
from buzzer import play_warning
from app_tracker import AppTracker

# ─── Logging Setup ────────────────────────────────────────────────────────────

LOG_DIR = Path.home() / ".guardian-shield" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            str(LOG_DIR / f"agent_{datetime.now().strftime('%Y%m%d')}.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("guardian.main")

# ─── Liveness tuning ──────────────────────────────────────────────────────────

# How often the agent emits a standalone heartbeat. Must be comfortably below the
# server's stale-device threshold (20s) so a slow or failed screen capture can no
# longer make the device look offline.
HEARTBEAT_INTERVAL_SEC = 5.0

# If the server never acknowledges an in-flight frame within this window, stop
# waiting for the ack and send the next frame anyway.
SEND_ACK_TIMEOUT_SEC = 2.5

# ─── Configuration ────────────────────────────────────────────────────────────

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.json"


def get_config_path() -> Path:
    """Return config path, allowing per-run override for local/demo modes."""
    override = os.environ.get("GUARDIAN_CONFIG_PATH", "").strip()
    if override:
        return Path(override)
    return DEFAULT_CONFIG_PATH


def load_config() -> dict:
    """Load agent configuration from config.json."""
    defaults = {
        "server_url": "http://localhost:3000",
        "device_name": platform.node() or "unknown-device",
        "screenshot_interval": 5,
        "target_fps": 0,
        "screenshot_quality": 60,
        "max_width": 1920,
        "capture_mode": "screen",
        "window_title_contains": "",
        "window_title_excludes": [],
        "auto_launch_edge": True,
        "edge_dashboard_url": "http://localhost:3000",
        "edge_start_url": "https://www.bing.com",
        "safe_browse_url": "http://localhost:3000",
        "low_latency_mode": False,
        "auth_token": "",
        "enable_local_buzzer": True,
        "enable_block_page_server": True,
        "block_page_port": 80,
        "use_online_blocklist": True,
        "clear_guardian_hosts_on_start": False,
        "enable_download_guard": True,
        "blocklist_update_interval": 3600,
        "download_watch_paths": [],
        "quarantine_path": "",
        "log_level": "INFO",
        "stealth_mode": False,
    }

    try:
        config_path = get_config_path()
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                user_config = json.load(f)
            defaults.update(user_config)
            logger.info(f"Loaded config from {config_path}")
        else:
            logger.warning(f"Config not found at {config_path}, using defaults")
    except Exception as e:
        logger.error(f"Error loading config: {e}, using defaults")

    return defaults


# ─── Socket.IO Client ────────────────────────────────────────────────────────

class ServerConnection:
    """Manages the WebSocket connection to the Guardian Shield server."""

    def __init__(
        self,
        server_url: str,
        device_name: str,
        auth_token: str = "",
        low_latency_mode: bool = False,
    ):
        self.server_url = server_url
        self.device_name = device_name
        self.auth_token = auth_token
        self.low_latency_mode = bool(low_latency_mode)
        self._send_inflight = False
        # Wall-clock time the in-flight frame was emitted. Used to break the latch
        # if the server never acks — otherwise one lost ack freezes the stream
        # permanently (which looked exactly like a static mirror image).
        self._send_inflight_at = 0.0
        self._dropped_frames = 0
        self.sio = socketio.Client(
            reconnection=True,
            reconnection_attempts=0,  # infinite
            reconnection_delay=5,
            reconnection_delay_max=30,
            logger=False,
        )
        self._connected = False
        self._on_config_update = None
        self._on_blocklist_update = None
        self._setup_events()

    def _run_off_read_loop(self, label: str, fn, *args, **kwargs):
        """
        Run a socket handler's real work on a background thread.

        python-socketio's Client dispatches event handlers inline on its read-loop
        thread. Anything slow or blocking there stops the client answering engine.io
        PINGs, so the server drops the connection after pingTimeout and the device
        flaps offline. Every handler that touches the app tracker, the blocker or
        the registry goes through here.
        """
        def _worker():
            try:
                fn(*args, **kwargs)
            except Exception as e:
                logger.error(f"{label} handler failed: {e}")

        try:
            self.sio.start_background_task(_worker)
        except Exception as e:
            logger.error(f"Could not dispatch {label} to a worker thread: {e}")

    def _setup_events(self):
        @self.sio.event
        def connect():
            logger.info(f"Connected to server: {self.server_url}")
            self._connected = True
            # Register this device
            self.sio.emit("register_device", {
                "device_name": self.device_name,
                "platform": platform.system(),
                "hostname": platform.node(),
                "auth_token": self.auth_token,
            })

        @self.sio.event
        def disconnect():
            logger.warning("Disconnected from server")
            self._connected = False
            self._send_inflight = False

        @self.sio.event
        def connect_error(data):
            logger.error(f"Connection error: {data}")
            self._connected = False

        # Handle commands from the dashboard
        @self.sio.on("update_blocklist")
        def on_update_blocklist(data):
            domains = data.get("domains", []) or []
            logger.info(f"Received blocklist update: {len(domains)} domains")

            def _apply():
                ok = True
                if hasattr(self, '_blocker') and self._blocker:
                    ok = self._blocker.set_custom_domains(domains)
                    if ok is False:
                        self.send_alert(
                            alert_type="blocking_error",
                            message="Blocked sites could not be applied (run agent as Administrator)",
                            details={"reason": "hosts_write_failed"},
                        )
                if callable(self._on_blocklist_update):
                    try:
                        self._on_blocklist_update(domains, ok)
                    except Exception as e:
                        logger.error(f"Blocklist enforcement callback failed: {e}")

            # Writes the hosts file and may flush DNS — far too slow for the read loop.
            self._run_off_read_loop("update_blocklist", _apply)

        @self.sio.on("update_config")
        def on_update_config(data):
            logger.info(f"Received config update: {data}")
            if callable(self._on_config_update):
                try:
                    self._on_config_update(data)
                except Exception as e:
                    logger.error(f"Failed to apply remote config: {e}")

        # ── App Tracking & Remote Control Commands ──────────────────────────
        @self.sio.on("close_app")
        def on_close_app(data):
            logger.info(f"Received remote close_app command: {data}")
            pid = data.get("pid")
            process_name = data.get("process_name")
            if hasattr(self, "_app_tracker") and self._app_tracker:
                def _close():
                    ok = self._app_tracker.close_app(pid=pid, process_name=process_name)
                    logger.info(f"close_app result for {process_name or pid}: {ok}")

                self._run_off_read_loop("close_app", _close)

        @self.sio.on("set_app_timer")
        def on_set_app_timer(data):
            logger.info(f"Received remote set_app_timer: {data}")
            app_name = data.get("app_name") or ""
            process_name = data.get("process_name") or ""
            duration = int(data.get("duration_seconds") or 300)
            timer_id = data.get("timer_id")
            if hasattr(self, "_app_tracker") and self._app_tracker:
                self._run_off_read_loop(
                    "set_app_timer",
                    self._app_tracker.set_app_timer,
                    app_name=app_name,
                    process_name=process_name,
                    duration_seconds=duration,
                    timer_id=timer_id,
                )

        @self.sio.on("cancel_app_timer")
        def on_cancel_app_timer(data):
            logger.info(f"Received cancel_app_timer: {data}")
            timer_id_or_process = data.get("timer_id") or data.get("process_name")
            if hasattr(self, "_app_tracker") and self._app_tracker:
                self._run_off_read_loop(
                    "cancel_app_timer",
                    self._app_tracker.cancel_app_timer,
                    timer_id_or_process,
                )

        @self.sio.on("update_blocked_apps")
        def on_update_blocked_apps(data):
            apps = data.get("blocked_apps") or []
            logger.info(f"Received update_blocked_apps: {len(apps)} items")
            if hasattr(self, "_app_tracker") and self._app_tracker:
                # Scans and kills processes — never run this on the read loop.
                self._run_off_read_loop(
                    "update_blocked_apps", self._app_tracker.set_blocked_apps, apps
                )

        @self.sio.on("send_child_message")
        def on_send_child_message(data):
            logger.info(f"Received parent message for child: {data.get('title')}")
            title = data.get("title") or "Notice"
            message = data.get("message") or ""
            urgency = data.get("urgency") or "info"
            duration = int(data.get("duration") or 10)
            if hasattr(self, "_app_tracker") and self._app_tracker:
                def _show():
                    self._app_tracker.show_child_message(
                        title=title,
                        message=message,
                        urgency=urgency,
                        duration=duration,
                    )
                    self.sio.emit("child_message_delivered", {
                        "device_name": self.device_name,
                        "title": title,
                        "delivered_at": time.time(),
                    })

                # Spawns a Tkinter/notify-send popup — keep it off the read loop.
                self._run_off_read_loop("send_child_message", _show)

        @self.sio.on("request_running_apps")
        def on_request_running_apps(data=None):
            logger.info("Received request_running_apps from server")
            if hasattr(self, "_app_tracker") and self._app_tracker:
                def _report():
                    apps = self._app_tracker.get_running_apps(limit=50)
                    active = self._app_tracker.get_foreground_app()
                    timers = self._app_tracker.get_active_timers()
                    logger.info(f"Emitting running_apps_report ({len(apps)} running apps, active: {active.get('app_name')})")
                    self.sio.emit("running_apps_report", {
                        "device_name": self.device_name,
                        "active_app": active,
                        "running_apps": apps,
                        "active_timers": timers,
                        "timestamp": time.time(),
                    })

                self._run_off_read_loop("request_running_apps", _report)

        @self.sio.on("request_installed_apps")
        def on_request_installed_apps(data=None):
            logger.info("Received request_installed_apps from server")
            if hasattr(self, "_app_tracker") and self._app_tracker:
                force = bool(data.get("force") if isinstance(data, dict) else False)

                def _report():
                    installed = self._app_tracker.get_installed_apps(force_refresh=force)
                    logger.info(f"Emitting installed_apps_report ({len(installed)} apps discovered)")
                    self.sio.emit("installed_apps_report", {
                        "device_name": self.device_name,
                        "installed_apps": installed,
                        "timestamp": time.time(),
                    })

                # Registry triple-scan on Windows / .desktop walk on Linux — seconds.
                self._run_off_read_loop("request_installed_apps", _report)

    def connect(self):
        """Connect to the server (non-blocking)."""
        try:
            # Try default Engine.IO negotiation first (polling -> websocket upgrade)
            # for maximum compatibility across network/firewall setups.
            self.sio.connect(
                self.server_url,
                wait=False,
                wait_timeout=10,
            )
        except Exception as e:
            logger.error(f"Failed to connect with default transport negotiation: {e!r}")
            try:
                # Fallback: force polling when websocket upgrade is blocked.
                self.sio.connect(
                    self.server_url,
                    transports=["polling"],
                    wait=False,
                    wait_timeout=10,
                )
            except Exception as fallback_err:
                logger.error(f"Failed to connect with polling fallback: {fallback_err!r}")

    def send_screenshot(self, jpeg_bytes: bytes, timestamp: float, meta: dict = None) -> bool:
        """Send a screenshot to the server. Returns True if sent."""
        if not self._connected:
            return False

        if self.low_latency_mode and self._send_inflight:
            # Break a stuck latch: if the ack never arrived, stop waiting for it.
            if time.time() - self._send_inflight_at >= SEND_ACK_TIMEOUT_SEC:
                logger.warning(
                    f"No screenshot ack in {SEND_ACK_TIMEOUT_SEC:.1f}s — "
                    f"clearing in-flight latch and resuming the stream"
                )
                self._send_inflight = False
            else:
                # Drop stale frames under load to keep live output current.
                self._dropped_frames += 1
                if self._dropped_frames % 120 == 0:
                    logger.info(f"Dropped {self._dropped_frames} stale frames (low-latency mode)")
                return True

        try:
            # Encode as base64 for Socket.IO transport
            b64_data = base64.b64encode(jpeg_bytes).decode("ascii")
            payload = {
                "device_name": self.device_name,
                "image": b64_data,
                "timestamp": timestamp,
                "size": len(jpeg_bytes),
            }
            if isinstance(meta, dict) and meta:
                payload["meta"] = meta

            if self.low_latency_mode:
                self._send_inflight = True
                self._send_inflight_at = time.time()

                def _acked(*_):
                    self._send_inflight = False

                self.sio.emit("screenshot", payload, callback=_acked)
            else:
                self.sio.emit("screenshot", payload)
            return True
        except Exception as e:
            self._send_inflight = False
            logger.error(f"Failed to send screenshot: {e}")
            return False

    def send_heartbeat(self) -> bool:
        """
        Emit a tiny liveness ping. Deliberately independent of screen capture:
        previously the only thing that refreshed the server's `last_seen` was the
        screenshot event, so any capture stall made the device look offline.
        """
        if not self._connected:
            return False
        try:
            self.sio.emit("heartbeat", {
                "device_name": self.device_name,
                "timestamp": time.time(),
            })
            return True
        except Exception as e:
            logger.debug(f"Heartbeat emit failed: {e}")
            return False

    def send_alert(self, alert_type: str, message: str, details: dict = None):
        """Send an alert to the server."""
        if not self._connected:
            return

        try:
            self.sio.emit("alert", {
                "device_name": self.device_name,
                "type": alert_type,
                "message": message,
                "details": details or {},
                "timestamp": time.time(),
            })
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")

    def disconnect(self):
        """Disconnect from the server."""
        try:
            self.sio.disconnect()
        except Exception:
            pass
        self._send_inflight = False

    @property
    def connected(self) -> bool:
        return self._connected


# ─── Main Agent ──────────────────────────────────────────────────────────────

class GuardianAgent:
    """Main agent orchestrator — brings all modules together."""

    def __init__(self, config: dict):
        self.config = config
        self._running = False
        self.low_latency_mode = bool(config.get("low_latency_mode", False))
        self.enable_local_buzzer = bool(config.get("enable_local_buzzer", True))
        self.enable_block_page_server = bool(config.get("enable_block_page_server", True))
        self.use_online_blocklist = bool(config.get("use_online_blocklist", True))
        self.clear_guardian_hosts_on_start = bool(config.get("clear_guardian_hosts_on_start", False))
        self.enable_download_guard = bool(config.get("enable_download_guard", True))
        self.capture_mode = str(config.get("capture_mode", "screen") or "screen").lower()
        self.auto_launch_edge = bool(config.get("auto_launch_edge", self.capture_mode in ("edge_window", "window")))
        self.edge_dashboard_url = str(config.get("edge_dashboard_url", "http://localhost:3000") or "http://localhost:3000")
        self.edge_start_url = str(config.get("edge_start_url", "https://www.bing.com") or "https://www.bing.com")
        self.safe_browse_url = str(config.get("safe_browse_url", self.edge_dashboard_url) or self.edge_dashboard_url)
        self._hosts_blocking_ok = True
        self._last_forced_block_by_host = {}
        self._last_block_alert_count = {}
        self._last_popup_count = {}
        self._last_force_block_warning_at = 0.0
        self._block_http_port = int(config.get("block_page_port", 80) or 80)
        self._config_lock = threading.Lock()

        # Check stealth consent
        stealth_requested = config.get("stealth_mode", False)
        self.stealth_enabled = stealth_requested and check_stealth_consent()
        if stealth_requested and not self.stealth_enabled:
            logger.info("Stealth mode requested but not consented — running visible")

        # Initialize modules
        self.screenshotter = ScreenshotCapture(
            quality=config.get("screenshot_quality", 60),
            max_width=config.get("max_width", 1920),
            low_latency_mode=self.low_latency_mode,
            capture_mode=config.get("capture_mode", "screen"),
            window_title_contains=config.get("window_title_contains", ""),
            window_title_excludes=config.get("window_title_excludes", []),
        )
        self.blocker = WebsiteBlocker()
        self.blocklist_updater = BlocklistUpdater(
            self.blocker,
            interval=config.get("blocklist_update_interval", 3600)
        )
        self.download_guard = None
        if self.enable_download_guard:
            self.download_guard = DownloadGuard(
                watch_paths=config.get("download_watch_paths") or None,
                quarantine_path=config.get("quarantine_path") or None,
                on_threat=self._on_threat_detected,
                on_dangerous_ext=self._on_dangerous_file,
            )
        self.server = ServerConnection(
            server_url=config.get("server_url", "http://localhost:3000"),
            device_name=config.get("device_name", "unknown"),
            auth_token=config.get("auth_token", ""),
            low_latency_mode=self.low_latency_mode,
        )
        # Prime psutil CPU percentage sampling to avoid initial 0.0 reading.
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass

        # Initialize Application Tracker
        self.app_tracker = AppTracker(
            on_alert=self._on_app_alert,
            on_timer_status=self._on_app_timer_status,
            enable_buzzer=self.enable_local_buzzer,
        )
        # Link to server connection
        self.server._app_tracker = self.app_tracker
        self.server._blocker = self.blocker
        self.server._on_config_update = self._apply_remote_config
        self.server._on_blocklist_update = self._on_blocklist_update

        # Offline screenshot cache
        self.cache = ScreenshotCache()
        self.cache_uploader = CacheUploader(
            cache=self.cache,
            upload_fn=self._upload_cached_screenshot,
            check_interval=10.0,
        )

    def _apply_remote_config(self, data: dict):
        """Apply stream-tuning updates sent from dashboard."""
        cfg = data or {}

        with self._config_lock:
            if "target_fps" in cfg:
                fps = float(cfg.get("target_fps") or 0)
                self.config["target_fps"] = max(1.0, min(30.0, fps))

            if "screenshot_quality" in cfg:
                quality = int(cfg.get("screenshot_quality") or 60)
                self.config["screenshot_quality"] = max(20, min(90, quality))
                self.screenshotter.quality = self.config["screenshot_quality"]

            if "max_width" in cfg:
                width = int(cfg.get("max_width") or 1280)
                self.config["max_width"] = max(640, min(1920, width))
                self.screenshotter.max_width = self.config["max_width"]

            if "low_latency_mode" in cfg:
                self.low_latency_mode = bool(cfg.get("low_latency_mode"))
                self.config["low_latency_mode"] = self.low_latency_mode
                self.screenshotter.low_latency_mode = self.low_latency_mode
                self.server.low_latency_mode = self.low_latency_mode

            logger.info(
                "Applied stream config: "
                f"fps={self.config.get('target_fps', 0)}, "
                f"quality={self.config.get('screenshot_quality', 60)}, "
                f"max_width={self.config.get('max_width', 1920)}, "
                f"low_latency={self.config.get('low_latency_mode', False)}"
            )

    def _on_app_alert(self, alert_type: str, message: str, details: dict):
        """Callback from app_tracker when an app is blocked, closed, or timer expires."""
        logger.warning(f"App Tracker Alert: [{alert_type}] {message}")
        if self.enable_local_buzzer and alert_type in ("app_blocked", "app_timer_expired"):
            play_warning(2)
        self.server.send_alert(
            alert_type=alert_type,
            message=message,
            details=details or {},
        )

    def _on_app_timer_status(self, timer_data: dict):
        """Forward timer updates to server."""
        if self.server and self.server.connected:
            try:
                self.server.sio.emit("app_timer_update", {
                    "device_name": self.server.device_name,
                    "timer": timer_data,
                    "timestamp": time.time(),
                })
            except Exception as e:
                logger.debug(f"Timer status emit error: {e}")

    def _on_threat_detected(self, filepath: str, threat_name: str):
        """Callback when malware is detected in a download."""
        logger.critical(f"🚨 MALWARE: {threat_name} in {filepath}")
        if self.enable_local_buzzer:
            play_warning(3)  # LOUD alarm for malware
        self.server.send_alert(
            alert_type="malware",
            message=f"Malware detected: {threat_name}",
            details={"file": filepath, "threat": threat_name},
        )

    def _on_dangerous_file(self, filepath: str, extension: str):
        """Callback when a dangerous file type is downloaded."""
        logger.warning(f"⚠️ Dangerous file: {filepath} ({extension})")
        if self.enable_local_buzzer:
            play_warning(2)  # Medium alert for suspicious file
        self.server.send_alert(
            alert_type="dangerous_file",
            message=f"Dangerous file type downloaded: {extension}",
            details={"file": filepath, "extension": extension},
        )

    def _on_block_alert(self, host: str, count: int, level: int):
        """Callback from block_page_server when a blocked site is visited."""
        logger.warning(f"Blocked site visited: {host} (warning #{count}, level {level})")

        # Send parent notification for each unique warning step.
        last_sent = int(self._last_block_alert_count.get(host, 0) or 0)
        if count <= last_sent:
            return

        self._last_block_alert_count[host] = count

        self.server.send_alert(
            alert_type="blocked_site",
            message=f"Blocked site attempt {min(count, 3)}/3: {host}",
            details={"host": host, "warning_count": count, "level": level},
        )

        # Show NUH UH popup once per unique warning step.
        self._show_nuh_uh_popup(host, count)

    def _show_nuh_uh_popup(self, host: str, count: int):
        """Open NUH UH in a new browser window once per warning count."""
        if not host:
            return

        last_popup = int(self._last_popup_count.get(host, 0) or 0)
        if count <= last_popup:
            return

        self._last_popup_count[host] = count

        try:
            if self._block_http_port == 80:
                forced_url = f"http://127.0.0.1/__guardian_block?host={quote(host)}&popup=1"
            else:
                forced_url = f"http://127.0.0.1:{self._block_http_port}/__guardian_block?host={quote(host)}&popup=1"

            if platform.system() == "Windows":
                edge_cmd = self._resolve_edge_executable()
                if edge_cmd:
                    subprocess.Popen([
                        edge_cmd,
                        "--new-window",
                        "--ignore-certificate-errors",
                        "--allow-running-insecure-content",
                        forced_url,
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    os.startfile(f"microsoft-edge:{forced_url}")
            else:
                opened = self._open_linux_popup_url(forced_url)
                if not opened:
                    logger.warning("Could not open Linux NUH UH popup in desktop session")
        except Exception as e:
            now = time.time()
            if now - self._last_force_block_warning_at >= 15:
                self._last_force_block_warning_at = now
                logger.warning(f"Could not open NUH UH popup: {e}")

    def _screenshot_callback(self, jpeg_bytes: bytes, timestamp: float):
        """Called on each screenshot capture. Caches if server is down."""
        sent = self.server.send_screenshot(jpeg_bytes, timestamp, meta=self._collect_telemetry())
        if not sent:
            # Server unreachable — save locally
            self.cache.save(jpeg_bytes, timestamp)
            logger.debug(f"Screenshot cached locally ({self.cache.pending_count} pending)")

    def _upload_cached_screenshot(self, jpeg_bytes: bytes, timestamp: float) -> bool:
        """Callback for CacheUploader — tries to send a cached screenshot."""
        return self.server.send_screenshot(jpeg_bytes, timestamp)

    def _read_cpu_temp_c(self) -> Optional[float]:
        """Read CPU temperature in Celsius on Linux systems."""
        candidates = [
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/hwmon/hwmon0/temp1_input",
        ]
        for path in candidates:
            try:
                if not os.path.exists(path):
                    continue
                raw = Path(path).read_text(encoding="utf-8", errors="ignore").strip()
                if not raw:
                    continue
                value = float(raw)
                if value > 1000:
                    value /= 1000.0
                if 0 < value < 150:
                    return round(value, 1)
            except Exception:
                continue
        return None

    def _collect_telemetry(self) -> dict:
        """Collect lightweight device telemetry to attach to live screenshots."""
        data = {
            "platform": platform.system().lower(),
            "cpu_usage_pct": None,
            "cpu_temp_c": None,
            "memory_usage_pct": None,
            "active_app": None,
            "active_timers": [],
        }

        try:
            data["cpu_usage_pct"] = round(float(psutil.cpu_percent(interval=None)), 1)
        except Exception:
            pass

        try:
            data["memory_usage_pct"] = round(float(psutil.virtual_memory().percent), 1)
        except Exception:
            pass

        temp = self._read_cpu_temp_c()
        if temp is not None:
            data["cpu_temp_c"] = temp

        try:
            if hasattr(self, "app_tracker") and self.app_tracker:
                data["active_app"] = self.app_tracker.get_foreground_app()
                data["active_timers"] = self.app_tracker.get_active_timers()
        except Exception as e:
            logger.debug(f"Telemetry active app collection note: {e}")

        return data

    def _get_foreground_window_title(self) -> str:
        if platform.system() != "Windows":
            return ""
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return ""
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return ""
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            return buff.value or ""
        except Exception:
            return ""

    def _get_foreground_window_process_name(self) -> str:
        if platform.system() != "Windows":
            return ""
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return ""

            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return ""

            return (psutil.Process(pid.value).name() or "").lower()
        except Exception:
            return ""

    def _domain_match_keyword(self, domain: str) -> str:
        d = (domain or "").lower().strip()
        if not d:
            return ""
        if d.endswith("youtube.com") or d.endswith("youtu.be"):
            return "youtube"
        parts = [p for p in d.split(".") if p and p not in {"www", "com", "net", "org", "co", "io"}]
        return parts[0] if parts else ""

    def _on_blocklist_update(self, domains, apply_ok):
        """If user is already on a blocked Edge page, force-show NUH UH instantly."""
        if platform.system() != "Windows":
            return

        title = self._get_foreground_window_title().lower()
        proc_name = self._get_foreground_window_process_name()
        if proc_name != "msedge.exe" and "edge" not in title:
            return

        matched_host = None
        for domain in domains or []:
            keyword = self._domain_match_keyword(str(domain))
            if keyword and keyword in title:
                matched_host = str(domain)
                break

        if not matched_host:
            return

        self._force_block_page(matched_host)

    def _force_block_page(self, host: str):
        """Open local NUH UH page immediately for a blocked host."""
        if not host:
            return

        try:
            if self._block_http_port == 80:
                forced_url = f"http://127.0.0.1/__guardian_block?host={quote(host)}&popup=1"
            else:
                forced_url = f"http://127.0.0.1:{self._block_http_port}/__guardian_block?host={quote(host)}&popup=1"

            edge_cmd = self._resolve_edge_executable()
            if edge_cmd:
                subprocess.Popen([
                    edge_cmd,
                    "--new-window",
                    "--ignore-certificate-errors",
                    "--allow-running-insecure-content",
                    forced_url,
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.startfile(f"microsoft-edge:{forced_url}")

            logger.info(f"Forced immediate NUH UH page for active blocked site: {host}")
        except Exception as e:
            now = time.time()
            if now - self._last_force_block_warning_at >= 15:
                self._last_force_block_warning_at = now
                logger.warning(f"Could not force immediate NUH UH page: {e}")

    def _resolve_linux_browser_executable(self) -> Optional[str]:
        """Find a Chromium-compatible browser executable on Linux."""
        for candidate in [
            "chromium-browser",
            "chromium",
            "google-chrome",
            "microsoft-edge",
        ]:
            found = shutil.which(candidate)
            if found:
                return found
        return None

    def _linux_desktop_users(self) -> list[tuple[str, int]]:
        """Return likely interactive Linux desktop users as (username, uid)."""
        users: list[tuple[str, int]] = []
        seen = set()

        for run_user_dir in Path("/run/user").glob("*"):
            try:
                uid = int(run_user_dir.name)
                if uid <= 0:
                    continue
                name = pwd.getpwuid(uid).pw_name
                key = (name, uid)
                if key in seen:
                    continue
                seen.add(key)
                users.append(key)
            except Exception:
                continue

        for name in ["pi", "ubuntu"]:
            try:
                uid = pwd.getpwnam(name).pw_uid
                key = (name, uid)
                if key not in seen:
                    seen.add(key)
                    users.append(key)
            except Exception:
                continue

        return users

    def _open_linux_popup_url(self, url: str) -> bool:
        """Open popup URL on Linux from a root service into active desktop session."""
        browser_cmd = self._resolve_linux_browser_executable()
        if not browser_cmd:
            return False

        browser_args = [
            browser_cmd,
            "--new-window",
            "--ignore-certificate-errors",
            "--allow-running-insecure-content",
            url,
        ]

        # First, try launching as currently running user with common displays.
        for display in [":0", ":1", ":99"]:
            try:
                env = os.environ.copy()
                env.setdefault("DISPLAY", display)
                env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
                env.setdefault("WAYLAND_DISPLAY", "wayland-0")
                if os.path.exists("/run/user/1000/bus"):
                    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")

                subprocess.Popen(
                    browser_args,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except Exception:
                continue

        # If service runs as root, explicitly run browser as desktop user.
        if os.geteuid() == 0:
            sudo_cmd = shutil.which("sudo")
            if sudo_cmd:
                for username, uid in self._linux_desktop_users():
                    runtime = f"/run/user/{uid}"
                    bus = f"{runtime}/bus"
                    for display in [":0", ":1"]:
                        cmd = [
                            sudo_cmd,
                            "-u",
                            username,
                            "env",
                            f"DISPLAY={display}",
                            f"XDG_RUNTIME_DIR={runtime}",
                            "WAYLAND_DISPLAY=wayland-0",
                        ]
                        if os.path.exists(bus):
                            cmd.append(f"DBUS_SESSION_BUS_ADDRESS=unix:path={bus}")
                        cmd.extend(browser_args)
                        try:
                            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            return True
                        except Exception:
                            continue

        # Final fallback.
        try:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    def _edge_block_enforcer_loop(self):
        """Continuously enforce blocklist against active Edge tab titles."""
        while self._running:
            try:
                title = self._get_foreground_window_title().lower()
                proc_name = self._get_foreground_window_process_name()
                if proc_name != "msedge.exe" and "edge" not in title:
                    time.sleep(0.5)
                    continue

                matched_host = None
                for domain in sorted(self.blocker.custom_domains):
                    keyword = self._domain_match_keyword(domain)
                    if keyword and keyword in title:
                        matched_host = domain
                        break

                if matched_host:
                    now = time.time()
                    last = self._last_forced_block_by_host.get(matched_host, 0)
                    if now - last >= 2.0:
                        self._last_forced_block_by_host[matched_host] = now
                        self._force_block_page(matched_host)
            except Exception as e:
                logger.debug(f"Edge block enforcer loop error: {e}")

            time.sleep(0.4)

    def _launch_edge_windows_if_needed(self):
        if platform.system() != "Windows" or not self.auto_launch_edge:
            return
        edge_cmd = self._resolve_edge_executable()
        if not edge_cmd:
            try:
                os.startfile(f"microsoft-edge:{self.edge_dashboard_url}")
                time.sleep(0.8)
                os.startfile(f"microsoft-edge:{self.edge_start_url}")
                logger.info("Auto-launched Edge via microsoft-edge: protocol")
            except Exception:
                logger.warning("Microsoft Edge executable/protocol not available; skipping auto-launch")
            return
        try:
            subprocess.Popen([
                edge_cmd,
                "--new-window",
                "--ignore-certificate-errors",
                "--allow-running-insecure-content",
                self.edge_dashboard_url,
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.8)
            subprocess.Popen([
                edge_cmd,
                "--new-window",
                "--ignore-certificate-errors",
                "--allow-running-insecure-content",
                self.edge_start_url,
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("Auto-launched Edge dashboard + browsing windows")
        except Exception as e:
            logger.warning(f"Failed to auto-launch Edge windows: {e}")

    def _resolve_edge_executable(self) -> Optional[str]:
        """Find msedge.exe using PATH, registry, and common install locations."""
        edge_cmd = shutil.which("msedge")
        if edge_cmd:
            return edge_cmd

        candidates = [
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]

        try:
            import winreg  # type: ignore

            registry_keys = [
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
            ]
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for reg_path in registry_keys:
                    try:
                        with winreg.OpenKey(hive, reg_path) as key:
                            value, _ = winreg.QueryValueEx(key, None)
                            if isinstance(value, str) and value:
                                candidates.insert(0, value)
                    except OSError:
                        continue
        except Exception:
            pass

        seen = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            if os.path.exists(candidate):
                return candidate

        return None

    def _init_blocking_services(self):
        """Initialize website-blocking state without delaying screenshot startup."""
        try:
            logger.info("Initializing website blocker...")

            if self.use_online_blocklist:
                # Load cached blocklist first for quick start.
                self.blocker.load_cached_blocklist()
                if self.blocker.all_blocked:
                    self.blocker.apply()
                    logger.info(f"Applied {len(self.blocker.all_blocked)} cached blocked domains")

                # Start periodic blocklist updates.
                self.blocklist_updater.start()
            else:
                logger.info("Online blocklist updater disabled; using dashboard custom domains only")
        except Exception as e:
            logger.error(f"Website blocker init failed: {e}")

    def start(self):
        """Start all agent modules."""
        self._running = True
        logger.info("=" * 60)
        logger.info("Guardian Shield Agent Starting")
        logger.info(f"Platform: {platform.system()} {platform.release()}")
        logger.info(f"Device: {self.config.get('device_name')}")
        logger.info(f"Server: {self.config.get('server_url')}")
        logger.info(f"Capture mode: {self.config.get('capture_mode', 'screen')}")
        logger.info(f"Low latency mode: {'ON' if self.low_latency_mode else 'OFF'}")
        logger.info(f"Local buzzer: {'ON' if self.enable_local_buzzer else 'OFF'}")
        logger.info(f"Block page server: {'ON' if self.enable_block_page_server else 'OFF'}")
        logger.info(f"Online blocklist updates: {'ON' if self.use_online_blocklist else 'OFF'}")
        logger.info("=" * 60)

        # Write our PID
        _ensure_pid_dir()
        write_pid(AGENT_PID_FILE, os.getpid())

        # Clear previous Guardian hosts entries before connecting so incoming
        # blocklist updates are not wiped by later async initialization.
        if self.clear_guardian_hosts_on_start:
            if not self.blocker.remove_all():
                self._hosts_blocking_ok = False
                logger.error("Cannot modify hosts file. Falling back to active-tab block enforcement only.")
                self.server.send_alert(
                    alert_type="blocking_error",
                    message="Hosts blocking unavailable; using active-tab fallback only",
                    details={"reason": "hosts_cleanup_failed"},
                )
            self.blocker.blocklist_domains.clear()
            self.blocker.custom_domains.clear()

        # 1. Connect to server
        logger.info("Connecting to server...")
        self.server.connect()

        # 1b. Start cache uploader (uploads stored screenshots when online)
        self.cache_uploader.start()
        if self.cache.pending_count > 0:
            logger.info(f"Found {self.cache.pending_count} cached screenshots to upload")

        # 2. Start block-page intercept server (serves warning pages for blocked sites)
        if self.enable_block_page_server:
            try:
                block_port = int(self.config.get("block_page_port", 80) or 80)
            except Exception:
                block_port = 80
            logger.info(f"Starting block page server on port {block_port}...")
            def _start_block_server_worker():
                try:
                    srv = start_block_server(
                        port=block_port,
                        alert_callback=self._on_block_alert,
                        safe_url=self.safe_browse_url,
                    )
                    if srv:
                        try:
                            self._block_http_port = int(getattr(srv, "server_port", block_port))
                        except Exception:
                            self._block_http_port = block_port
                        logger.info(f"Block-page HTTP endpoint ready on :{self._block_http_port}")
                    else:
                        logger.error("Block page server could not bind on any HTTP port")
                        self.enable_block_page_server = False
                except Exception as e:
                    logger.error(f"Block page server failed to start: {e}")
                    self.enable_block_page_server = False

            threading.Thread(target=_start_block_server_worker, daemon=True, name="block-server-start").start()
            if block_port != 80:
                logger.warning("block_page_port is not 80; browser redirect from hosts file may not show warning page")
        else:
            logger.info("Block page server disabled by config")

        # 3. Start website blocking asynchronously to avoid delaying live stream startup.
        threading.Thread(target=self._init_blocking_services, daemon=True, name="blocker-init").start()

        # 3b. Start active-tab enforcer for immediate blocking in Edge sessions.
        threading.Thread(target=self._edge_block_enforcer_loop, daemon=True, name="edge-block-enforcer").start()

        # 3c. Auto-launch Edge windows for live mode usability.
        self._launch_edge_windows_if_needed()

        # 4. Start download guard
        if self.download_guard:
            logger.info("Starting download guard...")
            self.download_guard.start()
        else:
            logger.info("Download guard disabled by config")

        # 5. Start Application Tracker & periodic reporting
        logger.info("Starting application tracker...")
        self.app_tracker.start()
        threading.Thread(target=self._app_sync_loop, daemon=True, name="app-sync-loop").start()

        # 5b. Start the liveness heartbeat. Deliberately its own thread with no
        # dependency on capture, the app tracker or the blocker, so nothing slow
        # can make the device look offline.
        threading.Thread(target=self._heartbeat_loop, daemon=True, name="heartbeat").start()

        # 6. Start screenshot loop (blocking — runs forever)
        logger.info("Starting screenshot capture...")
        target_fps = float(self.config.get("target_fps", 0) or 0)
        if target_fps > 0:
            interval = max(0.01, 1.0 / target_fps)
        else:
            interval = float(self.config.get("screenshot_interval", 5))
        logger.info(f"Capture interval: {interval:.3f}s (~{1.0 / interval:.1f} FPS)")

        try:
            consecutive_capture_failures = 0
            while self._running:
                start_time = time.time()
                try:
                    jpeg = self.screenshotter.capture()
                except Exception as cap_err:
                    jpeg = None
                    logger.error(f"Screen capture raised: {cap_err}")

                if jpeg:
                    if consecutive_capture_failures:
                        logger.info(
                            f"Screen capture recovered after "
                            f"{consecutive_capture_failures} failed attempt(s)"
                        )
                        consecutive_capture_failures = 0
                    try:
                        self._screenshot_callback(jpeg, time.time())
                    except Exception as send_err:
                        # Never let a send/telemetry error kill the capture loop —
                        # that used to leave the mirror frozen on the last frame.
                        logger.error(f"Screenshot dispatch failed: {send_err}")
                else:
                    consecutive_capture_failures += 1
                    # Log on the 1st, 5th, then every 20th failure so a broken
                    # capture backend is visible in the journal instead of silent.
                    if consecutive_capture_failures in (1, 5) or consecutive_capture_failures % 20 == 0:
                        logger.warning(
                            f"Screen capture returned no frame "
                            f"({consecutive_capture_failures} consecutive). "
                            f"Live view will stay on the last good frame."
                        )
                elapsed = time.time() - start_time

                # Recompute interval dynamically so dashboard tuning applies live.
                target_fps = float(self.config.get("target_fps", 0) or 0)
                if target_fps > 0:
                    interval = max(0.01, 1.0 / target_fps)
                else:
                    interval = float(self.config.get("screenshot_interval", 5))

                sleep_time = max(0, interval - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
        except KeyboardInterrupt:
            logger.info("Agent interrupted by user")
        finally:
            self.stop()

    def _heartbeat_loop(self):
        """
        Emit a small heartbeat every HEARTBEAT_INTERVAL_SEC.

        This exists because the server used to infer liveness purely from incoming
        screenshots, so a slow capture (grim can take seconds on a Pi) or a capture
        failure looked identical to a dead device and tripped the 20s stale sweeper.
        """
        while self._running:
            try:
                if self.server:
                    self.server.send_heartbeat()
            except Exception as e:
                logger.debug(f"Heartbeat tick note: {e}")
            time.sleep(HEARTBEAT_INTERVAL_SEC)

    def _app_sync_loop(self):
        """Periodically report running apps and initial installed apps to the server."""
        time.sleep(2.0)  # Wait for socket connection to stabilize
        last_installed_sync = 0.0

        while self._running:
            try:
                if self.server and (self.server.connected or (hasattr(self.server, "sio") and self.server.sio.connected)):
                    now = time.time()
                    # 1. Sync installed apps once on startup & every 10 minutes
                    if now - last_installed_sync >= 600.0:
                        last_installed_sync = now
                        installed = self.app_tracker.get_installed_apps()
                        logger.info(f"Syncing {len(installed)} installed apps to server")
                        self.server.sio.emit("installed_apps_report", {
                            "device_name": self.server.device_name,
                            "installed_apps": installed,
                            "timestamp": now,
                        })

                    # 2. Sync running apps every 3.5 seconds
                    running = self.app_tracker.get_running_apps(limit=40)
                    active = self.app_tracker.get_foreground_app()
                    timers = self.app_tracker.get_active_timers()
                    self.server.sio.emit("running_apps_report", {
                        "device_name": self.server.device_name,
                        "active_app": active,
                        "running_apps": running,
                        "active_timers": timers,
                        "timestamp": now,
                    })
            except Exception as e:
                logger.debug(f"App sync loop tick note: {e}")

            time.sleep(3.5)

    def stop(self):
        """Stop all agent modules gracefully."""
        self._running = False
        logger.info("Stopping Guardian Shield Agent...")

        if hasattr(self, "app_tracker") and self.app_tracker:
            self.app_tracker.stop()
        self.cache_uploader.stop()
        if self.use_online_blocklist:
            self.blocklist_updater.stop()
        if self.download_guard:
            self.download_guard.stop()
        self.screenshotter.close()
        self.server.disconnect()

        logger.info(f"Agent stopped. ({self.cache.pending_count} screenshots cached for next run)")


def run_agent(as_service: bool = False, stop_event=None):
    """
    Main entry point for the agent.

    Args:
        as_service: True if running as a Windows service.
        stop_event: win32event handle for service stop.
    """
    config = load_config()
    logging.getLogger().setLevel(config.get("log_level", "INFO"))

    agent = GuardianAgent(config)

    if as_service and stop_event:
        # Running as Windows service — check stop event periodically
        import win32event
        threading.Thread(target=agent.start, daemon=True).start()
        win32event.WaitForSingleObject(stop_event, win32event.INFINITE)
        agent.stop()
    else:
        # Handle Ctrl+C
        def signal_handler(sig, frame):
            logger.info("Shutdown signal received")
            agent.stop()
            sys.exit(0)

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
        agent.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Guardian Shield Agent")
    parser.add_argument(
        "--no-watchdog", action="store_true",
        help="Don't start the watchdog process (used when launched BY the watchdog)"
    )
    parser.add_argument(
        "--install", action="store_true",
        help="Install as a system service"
    )
    parser.add_argument(
        "--uninstall", action="store_true",
        help="Uninstall the system service"
    )
    parser.add_argument(
        "--admin-consent", action="store_true",
        help="Admin (parent/boss) consents to stealth monitoring"
    )
    parser.add_argument(
        "--user-consent", action="store_true",
        help="User (child/employee) consents to stealth monitoring"
    )
    parser.add_argument(
        "--config", type=str,
        help="Path to config JSON (overrides default config.json)"
    )
    args = parser.parse_args()

    if args.config:
        os.environ["GUARDIAN_CONFIG_PATH"] = args.config

    if args.admin_consent:
        request_stealth_consent(is_admin_setup=True)
    elif args.user_consent:
        request_stealth_consent(is_admin_setup=False)
    elif args.install:
        from persistence import install
        install()
    elif args.uninstall:
        from persistence import uninstall
        uninstall()
    else:
        # Start watchdog if not told otherwise
        if not args.no_watchdog:
            try:
                start_watchdog_subprocess()
            except Exception as e:
                logger.warning(f"Could not start watchdog: {e}")

        run_agent()
