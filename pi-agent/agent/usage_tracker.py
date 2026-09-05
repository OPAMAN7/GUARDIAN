#!/usr/bin/env python3
"""
Guardian Agent — Foreground Usage Collector (source: "app")
===========================================================

Turns a stream of "what window is focused right now" samples into a small number
of closed *sessions*, and queues them for upload through the offline spool.

What this is NOT
----------------
This is not a second app tracker. ``AppTracker`` already owns process listing,
blocking and timers, and its ``get_foreground_app()`` mutates shared duration
state outside the lock with two existing callers. A third caller would corrupt
the numbers the shipped Apps page shows, so this module reads through the
side-effect-free ``get_foreground_snapshot()`` and keeps its own clock.

The three rules that shape the code
-----------------------------------
1. **Browser time is an envelope, never content.** "Chromium: 3h" and the web
   history rows for those same 3 hours describe the *same wall-clock seconds*.
   Sessions for browser processes are flagged ``is_browser=1`` and the server
   excludes them from category totals. They are still uploaded, because a
   browser envelope that is far larger than the sum of the web rows is the
   cheapest possible signal that history collection is broken.
2. **A session never straddles local midnight.** The rollup is keyed by local
   day, so a session running 23:40 -> 00:20 is split into two records. Anything
   else silently posts half of Tuesday's study into Monday.
3. **Time the agent could not observe is never credited.** If samples stop
   arriving (suspend, service restart, display gone), the open session is closed
   at the *last observed sample*, not at "now".
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

try:  # normal (flat, non-package) agent import
    from spool import Spool, local_day, utc_iso
except ImportError:  # pragma: no cover - test harness import
    from .spool import Spool, local_day, utc_iso  # type: ignore

logger = logging.getLogger(__name__)

# How often the focused window is sampled. 5 s matches the heartbeat cadence and
# keeps the per-session error under a rounding error for anything worth grading.
POLL_INTERVAL_SEC = 5.0

# A single unbroken session longer than this is split. Real focus sessions do not
# run for an hour without the window title changing; an hour-long block is much
# more likely to be a forgotten open window.
MAX_SESSION_SEC = 3600

# Sub-5 s focus is menu flicker and alt-tab noise, not usage.
MIN_SESSION_SEC = 5

# If the gap between two samples exceeds this, the agent was not watching and
# the unobserved time is discarded rather than credited.
SAMPLE_GAP_TOLERANCE_SEC = 25.0

# Idle handling (best effort — see _IdleProbe).
IDLE_THRESHOLD_SEC = 300
IDLE_CHECK_INTERVAL_SEC = 60.0

BROWSER_PROCESSES = {
    "chromium", "chromium-browser", "chrome", "google-chrome",
    "google-chrome-stable", "firefox", "firefox-esr", "msedge", "microsoft-edge",
    "microsoft-edge-stable", "brave", "brave-browser", "opera", "vivaldi",
    "epiphany", "epiphany-browser", "midori", "safari",
    # Windows process names arrive with the extension attached.
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
}

# Foreground readings that mean "nothing is focused". Counting these as usage
# turns an idle desktop into hours of "other".
NON_SESSION_PROCESSES = {
    "", "desktop", "explorer.exe", "unknown", "none",
}


def _norm_proc(name: Optional[str]) -> str:
    return (name or "").strip().lower()


def _midnight_after(ts: float) -> float:
    """Epoch of the next LOCAL midnight strictly after ``ts``."""
    lt = time.localtime(ts)
    # mktime normalises an out-of-range mday, so DST transitions are handled by
    # the platform rather than by arithmetic here.
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))


class _IdleProbe:
    """Best-effort desktop idle time in seconds, or None if unknowable.

    Fails open on purpose: when idle time cannot be read the collector counts the
    time exactly as it does today. Being wrong in the direction of "assume the
    child was there" is the behaviour the system already has.

    The binary lookup is re-probed periodically rather than cached for the whole
    process lifetime — a one-shot capability probe cached forever is precisely
    what made the live mirror silently dead in BUG-28.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = bool(enabled)
        self._binary: Optional[str] = None
        self._probed_at = 0.0
        self._probe_ttl = 600.0
        self._fail_count = 0
        self.available: Optional[bool] = None

    def _binary_path(self) -> Optional[str]:
        now = time.time()
        if self._binary is None or (now - self._probed_at) > self._probe_ttl:
            self._probed_at = now
            self._binary = shutil.which("xprintidle") or ""
        return self._binary or None

    def seconds(self) -> Optional[float]:
        if not self.enabled or platform.system() == "Windows":
            return None
        binary = self._binary_path()
        if not binary:
            self.available = False
            return None
        env = os.environ.copy()
        env.setdefault("DISPLAY", ":0")
        try:
            out = subprocess.run(
                [binary], capture_output=True, text=True, timeout=4, env=env
            )
            if out.returncode != 0 or not (out.stdout or "").strip().isdigit():
                self._fail_count += 1
                if self._fail_count == 3:
                    logger.info(
                        "Idle detection unavailable (xprintidle returned %s) — "
                        "usage time will not be idle-trimmed.", out.returncode
                    )
                self.available = False
                return None
            self.available = True
            self._fail_count = 0
            return int(out.stdout.strip()) / 1000.0
        except Exception:
            self._fail_count += 1
            self.available = False
            return None


class UsageTracker:
    """Collapses foreground samples into closed sessions on the spool."""

    def __init__(
        self,
        app_tracker,
        spool: Spool,
        poll_interval: float = POLL_INTERVAL_SEC,
        idle_detection: bool = True,
        idle_threshold_sec: int = IDLE_THRESHOLD_SEC,
        max_session_sec: int = MAX_SESSION_SEC,
        min_session_sec: int = MIN_SESSION_SEC,
    ):
        self.app_tracker = app_tracker
        self.spool = spool
        self.poll_interval = float(poll_interval)
        # Derived, not constant: a caller that samples every 60 s must not have
        # every single sample look like an unobserved gap. Four missed samples
        # (25 s at the default 5 s cadence) is the threshold.
        self.gap_tolerance_sec = max(SAMPLE_GAP_TOLERANCE_SEC, self.poll_interval * 4.0)
        self.idle_threshold_sec = int(idle_threshold_sec)
        self.max_session_sec = int(max_session_sec)
        self.min_session_sec = int(min_session_sec)
        self._idle = _IdleProbe(enabled=idle_detection)
        self._lock = threading.Lock()
        self._cur: Optional[Dict] = None
        self._last_idle_check = 0.0
        self.stats = {
            "sessions_recorded": 0,
            "sessions_dropped_short": 0,
            "idle_closes": 0,
            "gap_closes": 0,
            "midnight_splits": 0,
            "samples": 0,
        }

    # ─── core state machine (pure enough to unit test) ──────────────────────

    def poll_once(self, now: Optional[float] = None,
                  snapshot: Optional[Dict] = None,
                  idle_sec: Optional[float] = -1.0) -> None:
        """Advance the state machine by one sample.

        ``snapshot`` and ``idle_sec`` exist so tests can drive this without a
        desktop. ``idle_sec=-1`` means "go and measure it".
        """
        now = time.time() if now is None else now
        if snapshot is None:
            try:
                snapshot = self.app_tracker.get_foreground_snapshot()
            except Exception as exc:
                logger.debug("Foreground snapshot failed: %s", exc)
                snapshot = None

        with self._lock:
            self.stats["samples"] += 1
            proc = _norm_proc((snapshot or {}).get("process_name"))
            focused = bool(snapshot) and proc not in NON_SESSION_PROCESSES

            # 1. Unobserved gap: close at the last sample we actually saw.
            cur = self._cur
            if cur and (now - cur["last_ts"]) > self.gap_tolerance_sec:
                self.stats["gap_closes"] += 1
                self._close_locked(cur["last_ts"], reason="gap")
                cur = None

            # 2. Idle: close the session and do not credit the idle stretch.
            if cur and self._should_check_idle(now, idle_sec):
                measured = self._idle.seconds() if idle_sec == -1.0 else idle_sec
                self._last_idle_check = now
                if measured is not None and measured >= self.idle_threshold_sec:
                    end = max(cur["start_ts"], now - measured)
                    self.stats["idle_closes"] += 1
                    self._close_locked(end, reason="idle")
                    cur = None

            if not focused:
                if cur:
                    self._close_locked(now, reason="unfocused")
                return

            # 3. Same app: extend, but honour midnight and the session cap.
            if cur and cur["proc"] == proc:
                boundary = _midnight_after(cur["start_ts"])
                if now >= boundary:
                    self.stats["midnight_splits"] += 1
                    self._close_locked(boundary - 1, reason="midnight")
                    self._open_locked(snapshot, proc, boundary, now)
                    return
                if (now - cur["start_ts"]) >= self.max_session_sec:
                    self._close_locked(now, reason="cap")
                    self._open_locked(snapshot, proc, now, now)
                    return
                cur["last_ts"] = now
                cur["samples"] += 1
                if not cur["is_browser"]:
                    title = (snapshot or {}).get("window_title") or ""
                    if title and len(title) > len(cur["window_title"] or ""):
                        cur["window_title"] = title[:300]
                return

            # 4. Different app: close the old one, start a new one.
            if cur:
                self._close_locked(now, reason="switch")
            self._open_locked(snapshot, proc, now, now)

    def _should_check_idle(self, now: float, idle_sec: Optional[float]) -> bool:
        if idle_sec != -1.0:
            return True  # test-injected value, always evaluate
        if not self._idle.enabled:
            return False
        return (now - self._last_idle_check) >= IDLE_CHECK_INTERVAL_SEC

    def _open_locked(self, snapshot: Dict, proc: str, start_ts: float,
                     last_ts: float) -> None:
        is_browser = proc in BROWSER_PROCESSES
        # Browser window titles are the page title — that is web-history content
        # arriving through the wrong source. Dropped here so the app ledger can
        # never contain what the web ledger owns.
        title = "" if is_browser else ((snapshot or {}).get("window_title") or "")[:300]
        self._cur = {
            "proc": proc,
            "app_name": ((snapshot or {}).get("app_name") or proc or "Unknown")[:120],
            "process_name": ((snapshot or {}).get("process_name") or proc)[:120],
            "window_title": title,
            "start_ts": float(start_ts),
            "last_ts": float(last_ts),
            "is_browser": is_browser,
            "platform": ((snapshot or {}).get("platform") or platform.system().lower()),
            "samples": 1,
        }

    def _close_locked(self, end_ts: float, reason: str = "") -> None:
        cur = self._cur
        self._cur = None
        if not cur:
            return
        duration = int(max(0.0, end_ts - cur["start_ts"]))
        if duration < self.min_session_sec:
            self.stats["sessions_dropped_short"] += 1
            return

        started_iso = utc_iso(cur["start_ts"])
        day = local_day(cur["start_ts"])
        key_src = f"{cur['proc']}|{started_iso}|{day}"
        record = {
            "kind": "usage",
            "session_key": hashlib.sha1(key_src.encode("utf-8")).hexdigest()[:24],
            "app_name": cur["app_name"],
            "process_name": cur["process_name"],
            "window_title": cur["window_title"] or None,
            "started_at": started_iso,
            "ended_at": utc_iso(end_ts),
            "duration_sec": duration,
            "day": day,
            "is_browser": 1 if cur["is_browser"] else 0,
            "platform": cur["platform"],
            "samples": cur["samples"],
            "close_reason": reason,
        }
        if self.spool.append(record):
            self.stats["sessions_recorded"] += 1

    # ─── lifecycle ──────────────────────────────────────────────────────────

    def flush(self) -> None:
        """Close any open session — called on shutdown so the last block counts."""
        with self._lock:
            if self._cur:
                self._close_locked(self._cur["last_ts"], reason="shutdown")

    def run_loop(self, is_running: Callable[[], bool]) -> None:
        """Daemon-thread body. Never raises."""
        logger.info("Usage collector started (poll %.1fs, idle trim %s)",
                    self.poll_interval,
                    "on" if self._idle.enabled else "off")
        while True:
            try:
                if not is_running():
                    break
                self.poll_once()
            except Exception as exc:  # never let this thread die
                logger.debug("Usage collector tick note: %s", exc)
            time.sleep(self.poll_interval)
        try:
            self.flush()
        except Exception:
            pass
        logger.info("Usage collector stopped (%s)", self.stats)
