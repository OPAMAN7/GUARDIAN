"""
Guardian Shield — Screenshot Capture Module
Cross-platform screen capture using mss library.
Falls back to scrot on Linux (Raspberry Pi / headless Xvfb).
Captures screenshots at configurable intervals and returns compressed JPEG bytes.
"""

import io
import os
import time
import logging
import platform
import subprocess
import tempfile
import shutil
import ctypes
from ctypes import wintypes
from typing import Optional
from pathlib import Path

from PIL import Image

try:
    from PIL import ImageGrab
    IMAGEGRAB_AVAILABLE = True
except Exception:
    ImageGrab = None
    IMAGEGRAB_AVAILABLE = False

# Try to import mss — not available on headless ARM without display
try:
    import mss
    import mss.tools
    MSS_AVAILABLE = True
except ImportError:
    MSS_AVAILABLE = False

logger = logging.getLogger("guardian.screenshot")

# How often the Wayland/grim probe may re-run while no working backend is known.
# Detection is cheap (a couple of stat() calls) but not free, and the capture loop
# can ask ~20x/second, so throttle it.
WAYLAND_REDETECT_SEC = 5.0


class ScreenshotCapture:
    """Handles periodic screen capture with JPEG compression.
    
    Uses mss on Windows/macOS, and scrot fallback on headless Linux (Raspberry Pi).
    """

    def __init__(
        self,
        quality: int = 60,
        monitor_index: int = 0,
        max_width: int = 1920,
        low_latency_mode: bool = False,
        capture_mode: str = "screen",
        window_title_contains: str = "",
        window_title_excludes=None,
    ):
        """
        Args:
            quality: JPEG compression quality (1-100). Lower = smaller files.
            monitor_index: 0 = all monitors combined, 1+ = specific monitor.
            max_width: Resize width cap for transport efficiency.
            low_latency_mode: Prioritize frame time over JPEG optimization ratio.
            capture_mode: "screen" (default) or "edge_window"/"window".
            window_title_contains: Match substring for window capture target.
            window_title_excludes: Optional list of title substrings to skip.
        """
        self.quality = quality
        self.max_width = max(640, int(max_width or 1920))
        self.low_latency_mode = bool(low_latency_mode)
        self.monitor_index = monitor_index
        self.capture_mode = (capture_mode or "screen").strip().lower()
        if self.capture_mode not in ("screen", "window", "edge_window"):
            self.capture_mode = "screen"

        self.window_title_contains = (window_title_contains or "").strip().lower()
        if self.capture_mode == "edge_window" and not self.window_title_contains:
            self.window_title_contains = "microsoft edge"
        self.window_title_excludes = [
            str(v).strip().lower() for v in (window_title_excludes or []) if str(v).strip()
        ]

        self._sct = None
        self._use_scrot = (not MSS_AVAILABLE) or (
            platform.system() == "Linux" and not os.environ.get("DISPLAY")
        )
        self._window_capture_enabled = (
            platform.system() == "Windows"
            and IMAGEGRAB_AVAILABLE
            and self.capture_mode in ("window", "edge_window")
        )
        self._wayland_grim_target = None
        self._wayland_detect_at = 0.0
        self._refresh_wayland_target(force=True, quiet=True)
        # On Pi with Xvfb, DISPLAY=:99 is set by start_agent.sh
        if platform.system() == "Linux" and os.environ.get("DISPLAY"):
            self._use_scrot = False  # mss can work if DISPLAY is set

        if self._use_scrot:
            logger.info("Using scrot fallback for headless Linux capture")
        else:
            logger.info("Using mss for screen capture")

        if self.low_latency_mode:
            logger.info(
                f"Low-latency capture enabled (max_width={self.max_width}, quality={self.quality})"
            )

        if self._window_capture_enabled:
            logger.info(
                "Window capture mode enabled "
                f"(contains='{self.window_title_contains or '*'}', excludes={self.window_title_excludes})"
            )

        if self._wayland_grim_target:
            run_as = self._wayland_grim_target.get("user") or "current-user"
            logger.info(
                "Wayland capture enabled via grim "
                f"(run_as={run_as}, display={self._wayland_grim_target.get('display')})"
            )

    def _encode_image(self, img: Image.Image) -> bytes:
        """Resize and JPEG-encode an image for transport."""
        if img.mode != "RGB":
            img = img.convert("RGB")

        if img.width > self.max_width:
            ratio = self.max_width / img.width
            img = img.resize((self.max_width, int(img.height * ratio)), Image.Resampling.LANCZOS)

        buffer = io.BytesIO()
        img.save(
            buffer,
            format="JPEG",
            quality=self.quality,
            optimize=not self.low_latency_mode,
            progressive=False,
        )
        return buffer.getvalue()

    def _get_window_title(self, hwnd) -> str:
        user32 = ctypes.windll.user32
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        return buff.value.strip()

    def _window_title_matches(self, title: str) -> bool:
        if not title:
            return False
        title_l = title.lower()
        if self.window_title_contains and self.window_title_contains not in title_l:
            return False
        for blocked in self.window_title_excludes:
            if blocked and blocked in title_l:
                return False
        return True

    def _get_window_rect(self, hwnd):
        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        ok = user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if not ok:
            return None

        left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        width = right - left
        height = bottom - top
        if width < 120 or height < 120:
            return None
        return (left, top, right, bottom)

    def _collect_candidate_windows(self):
        user32 = ctypes.windll.user32
        candidates = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def enum_proc(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.IsIconic(hwnd):
                return True

            title = self._get_window_title(hwnd)
            if not self._window_title_matches(title):
                return True

            rect = self._get_window_rect(hwnd)
            if not rect:
                return True

            area = (rect[2] - rect[0]) * (rect[3] - rect[1])
            candidates.append((hwnd, title, rect, area))
            return True

        user32.EnumWindows(enum_proc, 0)
        candidates.sort(key=lambda item: item[3], reverse=True)
        return candidates

    def _capture_with_window(self) -> Optional[bytes]:
        """Capture a specific app window (Windows only)."""
        if not self._window_capture_enabled:
            return None

        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            foreground_title = self._get_window_title(hwnd) if hwnd else ""

            target_rect = None
            target_title = ""

            if hwnd and self._window_title_matches(foreground_title):
                target_rect = self._get_window_rect(hwnd)
                target_title = foreground_title

            if not target_rect:
                candidates = self._collect_candidate_windows()
                if not candidates:
                    return None
                _, target_title, target_rect, _ = candidates[0]

            try:
                img = ImageGrab.grab(bbox=target_rect, all_screens=True)
            except TypeError:
                img = ImageGrab.grab(bbox=target_rect)

            jpeg_bytes = self._encode_image(img)
            logger.debug(
                f"window capture: '{target_title}' {target_rect[2]-target_rect[0]}x{target_rect[3]-target_rect[1]}, "
                f"{len(jpeg_bytes)/1024:.1f} KB"
            )
            return jpeg_bytes
        except Exception as e:
            logger.error(f"window capture failed: {e}")
            return None

    def _get_sct(self):
        """Lazy-init mss instance (must be created per-thread)."""
        if not MSS_AVAILABLE:
            return None
        if self._sct is None:
            self._sct = mss.mss()
        return self._sct

    def _refresh_wayland_target(self, force: bool = False, quiet: bool = False) -> None:
        """
        Re-run Wayland detection and adopt the result.

        This used to be a single probe in __init__, and that one-shot cache was why
        the live view could stay blank forever on a Pi that was otherwise perfectly
        healthy. Under systemd the agent is started at boot, often *before* the
        desktop compositor has created /run/user/<uid>/wayland-0. The probe found no
        socket, cached None, and capture() then skipped the Wayland branch for the
        rest of the process lifetime — every frame fell through to scrot, which
        cannot capture a Wayland session. Because the heartbeat is deliberately
        independent of capture, the dashboard still showed the device online, so the
        symptom was "device online, mirror never paints", and waiting did not help
        because nothing ever re-probed.

        Re-detecting also covers the reverse case: the child logs out or the
        compositor restarts, the old socket path goes stale, and we need to pick up
        the new one instead of failing on a dead target.
        """
        now = time.time()
        if not force and (now - self._wayland_detect_at) < WAYLAND_REDETECT_SEC:
            return
        self._wayland_detect_at = now

        try:
            target = self._detect_wayland_grim_target()
        except Exception as e:
            logger.debug(f"Wayland capture detection failed: {e}")
            return

        if target == self._wayland_grim_target:
            return

        previous = self._wayland_grim_target
        self._wayland_grim_target = target
        if quiet:
            return

        if target:
            logger.info(
                "Wayland capture target acquired via grim "
                f"(run_as={target.get('user') or 'current-user'}, "
                f"display={target.get('display')})"
            )
        elif previous:
            logger.warning(
                "Wayland capture target disappeared "
                f"(was {previous.get('runtime_dir')}/{previous.get('display')}); "
                "will keep probing"
            )

    def _detect_wayland_grim_target(self):
        """Find a Wayland runtime/display pair usable by grim."""
        if platform.system() != "Linux":
            return None
        if shutil.which("grim") is None:
            return None

        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
        wayland_display = os.environ.get("WAYLAND_DISPLAY", "").strip()
        if runtime_dir and wayland_display and Path(runtime_dir, wayland_display).exists():
            return {
                "runtime_dir": runtime_dir,
                "display": wayland_display,
                "user": None,
            }

        run_user_root = Path("/run/user")
        if not run_user_root.exists():
            return None

        candidates = []
        for user_dir in sorted(run_user_root.iterdir(), key=lambda p: p.name):
            if not user_dir.is_dir() or not user_dir.name.isdigit():
                continue

            sockets = sorted(user_dir.glob("wayland-*"))
            if not sockets:
                continue

            user_name = None
            try:
                import pwd
                user_name = pwd.getpwuid(int(user_dir.name)).pw_name
            except Exception:
                user_name = None

            candidates.append(
                {
                    "runtime_dir": str(user_dir),
                    "display": sockets[0].name,
                    "user": user_name,
                }
            )

        if not candidates:
            return None

        for candidate in candidates:
            if candidate.get("user") and candidate.get("user") != "root":
                return candidate

        return candidates[0]

    def _capture_with_grim(self) -> Optional[bytes]:
        """Capture using grim on Wayland sessions (Raspberry Pi OS Bookworm default)."""
        if not self._wayland_grim_target:
            return None

        tmp_path = None
        try:
            runtime_dir = self._wayland_grim_target["runtime_dir"]
            wayland_display = self._wayland_grim_target["display"]
            target_user = self._wayland_grim_target.get("user")
            use_sudo_user = hasattr(os, "geteuid") and os.geteuid() == 0 and target_user and target_user != "root"

            # Under some systemd environments, /tmp can be private/non-writable
            # for the sudo target user. Use that user's runtime dir instead.
            if use_sudo_user:
                output_dir = runtime_dir
            else:
                output_dir = tempfile.gettempdir()

            tmp_path = os.path.join(output_dir, f"guardian_grim_{int(time.time() * 1000)}.png")

            try:
                os.unlink(tmp_path)
            except Exception:
                pass

            if use_sudo_user:
                result = subprocess.run(
                    [
                        "sudo",
                        "-u",
                        target_user,
                        "env",
                        f"XDG_RUNTIME_DIR={runtime_dir}",
                        f"WAYLAND_DISPLAY={wayland_display}",
                        "grim",
                        tmp_path,
                    ],
                    timeout=10,
                    capture_output=True,
                )
            else:
                env = os.environ.copy()
                env["XDG_RUNTIME_DIR"] = runtime_dir
                env["WAYLAND_DISPLAY"] = wayland_display
                result = subprocess.run(
                    ["grim", tmp_path],
                    env=env,
                    timeout=10,
                    capture_output=True,
                )

            if result.returncode != 0:
                err = (result.stderr or b"").decode(errors="ignore").strip()
                logger.error(f"grim capture failed: {err or 'unknown error'}")
                return None

            img = Image.open(tmp_path).convert("RGB")
            jpeg_bytes = self._encode_image(img)
            logger.debug(f"grim capture: {img.width}x{img.height}, {len(jpeg_bytes)/1024:.1f} KB")
            return jpeg_bytes

        except Exception as e:
            logger.error(f"grim capture failed: {e}")
            return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def _capture_with_scrot(self) -> Optional[bytes]:
        """Capture using scrot command-line tool (Linux fallback)."""
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = tmp.name
            
            # Remove the 0-byte placeholder so scrot creates a fresh image
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

            env = os.environ.copy()
            if not env.get("DISPLAY") and os.path.exists("/tmp/.X11-unix/X0"):
                env["DISPLAY"] = ":0"
            if not env.get("XAUTHORITY") and os.path.exists("/home/pi/.Xauthority"):
                env["XAUTHORITY"] = "/home/pi/.Xauthority"

            result = subprocess.run(
                ["scrot", "--overwrite", tmp_path],
                env=env,
                timeout=5,
                capture_output=True
            )
            
            if result.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                err = (result.stderr or b"").decode(errors="ignore").strip()
                if err:
                    logger.debug(f"scrot failed: {err}")
                return None
            
            img = Image.open(tmp_path).convert("RGB")
            try:
                os.unlink(tmp_path)
                tmp_path = None
            except Exception:
                pass

            jpeg_bytes = self._encode_image(img)
            logger.debug(f"scrot capture: {img.width}x{img.height}, {len(jpeg_bytes)/1024:.1f} KB")
            return jpeg_bytes

        except FileNotFoundError:
            logger.debug("scrot not installed.")
            return None
        except Exception as e:
            logger.debug(f"scrot capture failed: {e}")
            return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def _capture_with_mss(self) -> Optional[bytes]:
        """Capture using mss library."""
        try:
            sct = self._get_sct()
            monitors = sct.monitors

            if self.monitor_index >= len(monitors):
                logger.warning(f"Monitor index {self.monitor_index} out of range, using 0")
                idx = 0
            else:
                idx = self.monitor_index

            screenshot = sct.grab(monitors[idx])
            img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")

            jpeg_bytes = self._encode_image(img)

            logger.debug(f"mss capture: {img.width}x{img.height}, {len(jpeg_bytes)/1024:.1f} KB")
            return jpeg_bytes

        except Exception as e:
            logger.error(f"mss capture failed: {e}")
            self._sct = None
            return None

    def capture(self) -> Optional[bytes]:
        """
        Capture the screen and return compressed JPEG bytes.
        Automatically selects mss or scrot based on platform/availability.

        Returns:
            JPEG image bytes, or None if capture fails.
        """
        if self._window_capture_enabled:
            window_bytes = self._capture_with_window()
            if window_bytes:
                return window_bytes

        # Re-probe while we have no Wayland target: the compositor may simply not
        # have been up yet when the service started. Throttled internally.
        if not self._wayland_grim_target:
            self._refresh_wayland_target()

        if self._wayland_grim_target:
            grim_bytes = self._capture_with_grim()
            if grim_bytes:
                return grim_bytes
            # grim was available but produced nothing — the session may have been
            # replaced. Re-probe so a new socket is picked up instead of retrying a
            # dead one until the process restarts.
            self._refresh_wayland_target()

        if self._use_scrot:
            return self._capture_with_scrot()
        else:
            result = self._capture_with_mss()
            if result is None:
                # Auto-fallback to scrot if mss fails at runtime
                if platform.system() == "Linux":
                    logger.warning("mss failed, trying scrot fallback...")
                    self._use_scrot = True
                    return self._capture_with_scrot()
                logger.warning("mss failed on non-Linux system; skipping frame")
                return None
            return result

    def close(self):
        """Clean up mss resources."""
        if self._sct is not None:
            self._sct.close()
            self._sct = None


def capture_loop(interval: float = 5.0, quality: int = 60, callback=None):
    """
    Continuously capture screenshots at the given interval.

    Args:
        interval: Seconds between captures.
        quality: JPEG quality.
        callback: Function to call with (jpeg_bytes, timestamp) on each capture.
    """
    capturer = ScreenshotCapture(quality=quality)
    logger.info(f"Starting screenshot loop: every {interval}s, quality={quality}")

    try:
        while True:
            start = time.time()
            jpeg_bytes = capturer.capture()
            if jpeg_bytes and callback:
                callback(jpeg_bytes, time.time())
            elapsed = time.time() - start
            sleep_time = max(0, interval - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)
    except KeyboardInterrupt:
        logger.info("Screenshot loop interrupted")
    finally:
        capturer.close()


if __name__ == "__main__":
    # Quick test: capture one screenshot and save to file
    logging.basicConfig(level=logging.DEBUG)
    cap = ScreenshotCapture(quality=60)
    data = cap.capture()
    if data:
        with open("test_screenshot.jpg", "wb") as f:
            f.write(data)
        print(f"Saved test_screenshot.jpg ({len(data) / 1024:.1f} KB)")
    cap.close()
