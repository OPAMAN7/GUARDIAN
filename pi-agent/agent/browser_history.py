#!/usr/bin/env python3
"""
Guardian Agent — Browser History Collector (sources: "web" and "watch")
======================================================================

Reads Chromium- and Firefox-family history databases on the monitored machine
and queues visit records for upload. Two logical sources come out of one
collector: ordinary web visits, and YouTube watch events (which carry a video id
and a media kind so the server can grade them on their own scale).

Why read the database instead of hooking the browser
----------------------------------------------------
An extension can be uninstalled by the child in two clicks and needs per-browser
packaging. The history database is written by the browser itself, is complete,
and survives the browser being killed.

The three things that make this safe
------------------------------------
1. **Never touch the live file.** The browser holds it open, and a wrong move
   corrupts the child's history. Every read happens on a throwaway *copy*
   (including the -wal/-shm sidecars, so nothing recent is missed).
2. **Per-profile watermarks.** Each profile records the timestamp of the last
   visit consumed. Nothing is read twice, and the visit key is derived from
   (browser, profile, visit id, timestamp) so a spool replay is still idempotent.
3. **The newest visit is deliberately held back.** Dwell time for a visit is
   inferred from when the *next* visit starts, so the most recent row has no
   measurable dwell yet. Emitting it early would bake in a wrong number that the
   server's INSERT OR IGNORE can never correct. It is released on the next poll,
   or immediately once the browser has clearly stopped (settle window elapsed).

Privacy
-------
Query strings are discarded — search terms are never stored or transmitted. The
single exception is the YouTube ``v`` parameter, which is the video identity and
the only way to tell one watch event from another. Page titles are stored locally
because YouTube can only be classified by title; whether titles are ever sent to
an AI provider is a separate server-side switch that defaults to off.
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

try:
    from spool import Spool, local_day, utc_iso
except ImportError:  # pragma: no cover
    from .spool import Spool, local_day, utc_iso  # type: ignore

logger = logging.getLogger(__name__)

# Chromium stores time as microseconds since 1601-01-01 (the Windows FILETIME
# epoch); Firefox uses microseconds since the Unix epoch.
CHROME_EPOCH_OFFSET_SEC = 11_644_473_600

POLL_INTERVAL_SEC = 120.0

# A single page visit contributing more than 30 minutes is almost always a tab
# left open, not reading.
DWELL_CAP_SEC = 1800
# Dwell assumed for a visit with no successor once the settle window has passed.
TAIL_DWELL_SEC = 60
# How long the newest visit is held back waiting for a successor.
TAIL_SETTLE_SEC = 1800

MAX_ROWS_PER_PROFILE = 4000

YOUTUBE_HOSTS = {
    "youtube.com", "m.youtube.com", "music.youtube.com",
    "youtube-nocookie.com", "youtu.be",
}

# Family -> (relative profile glob, history filename)
CHROMIUM_ROOTS = [
    (".config/chromium", "chromium"),
    (".config/google-chrome", "chrome"),
    (".config/google-chrome-beta", "chrome"),
    (".config/microsoft-edge", "edge"),
    (".config/BraveSoftware/Brave-Browser", "brave"),
    (".config/vivaldi", "vivaldi"),
    (".config/opera", "opera"),
    # Flatpak / snap installations keep their own home.
    (".var/app/org.chromium.Chromium/config/chromium", "chromium"),
    ("snap/chromium/common/chromium", "chromium"),
]

FIREFOX_ROOTS = [
    (".mozilla/firefox", "firefox"),
    (".var/app/org.mozilla.firefox/.mozilla/firefox", "firefox"),
    ("snap/firefox/common/.mozilla/firefox", "firefox"),
]

WINDOWS_CHROMIUM_ROOTS = [
    ("AppData/Local/Google/Chrome/User Data", "chrome"),
    ("AppData/Local/Microsoft/Edge/User Data", "edge"),
    ("AppData/Local/BraveSoftware/Brave-Browser/User Data", "brave"),
    ("AppData/Local/Chromium/User Data", "chromium"),
]


# ─── url helpers ────────────────────────────────────────────────────────────

def _strip_www(host: str) -> str:
    host = host.lower().strip()
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix) and host.count(".") >= 2:
            host = host[len(prefix):]
            break
    return host


def parse_url(url: str) -> Optional[Dict]:
    """Split a URL into the minimum we are willing to store.

    Returns None for anything that is not real browsing (about:, chrome://,
    file://, extension pages, the block page we serve ourselves).
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except Exception:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    host = _strip_www(parts.hostname or "")
    if not host or host in ("localhost", "127.0.0.1", "::1"):
        return None

    path = parts.path or "/"
    if len(path) > 300:
        path = path[:300]

    info = {
        "domain": host,
        "url_path": path,
        "platform": "web",
        "media_id": None,
        "media_kind": None,
    }

    root = host[4:] if host.startswith("www.") else host
    if root in YOUTUBE_HOSTS or root.endswith(".youtube.com"):
        info.update(_parse_youtube(root, parts, path))
    return info


def _parse_youtube(host: str, parts, path: str) -> Dict:
    out = {"platform": "youtube", "media_id": None, "media_kind": None}
    segs = [s for s in path.split("/") if s]

    if host == "youtu.be":
        if segs:
            out["media_id"] = segs[0][:24]
            out["media_kind"] = "video"
        return out

    if segs:
        head = segs[0].lower()
        if head == "shorts" and len(segs) > 1:
            out["media_id"] = segs[1][:24]
            out["media_kind"] = "short"
            return out
        if head in ("embed", "live", "v") and len(segs) > 1:
            out["media_id"] = segs[1][:24]
            out["media_kind"] = "live" if head == "live" else "video"
            return out

    # /watch?v=<id> — the ONLY query parameter ever read or stored.
    try:
        qs = parse_qs(parts.query or "")
        vid = (qs.get("v") or [None])[0]
        if vid:
            out["media_id"] = vid[:24]
            out["media_kind"] = "video"
    except Exception:
        pass
    return out


def clean_title(title: Optional[str], platform_hint: str = "web") -> Optional[str]:
    if not title:
        return None
    t = unquote(str(title)).strip()
    for suffix in (" - YouTube", " — YouTube", " - YouTube Music"):
        if t.endswith(suffix):
            t = t[: -len(suffix)].strip()
    return t[:300] or None


# ─── profile discovery ──────────────────────────────────────────────────────

def _isdir(p: Path) -> bool:
    """``Path.is_dir`` that cannot raise.

    Every probe below walks directories the agent does not own — other users'
    homes, autofs mounts, snap wrappers. A bare ``PermissionError`` from one of
    them used to abort the whole discovery pass, which meant a single
    mode-0700 home directory switched history collection off completely.
    """
    try:
        return p.is_dir()
    except OSError:
        return False


def _isfile(p: Path) -> bool:
    try:
        return p.is_file()
    except OSError:
        return False


def _listdir(p: Path) -> List[str]:
    try:
        return sorted(os.listdir(p))
    except OSError:
        return []


def candidate_homes(extra: Optional[Iterable[str]] = None) -> List[Path]:
    """Every home directory that might hold a browser profile.

    Under systemd the agent runs as root, so ``Path.home()`` is ``/root`` while
    the child's profile lives in ``/home/pi``. Missing this is the single most
    likely reason for "history collection found nothing".
    """
    homes: List[Path] = []
    seen = set()

    def _add(p) -> None:
        try:
            path = Path(p).expanduser()
        except Exception:
            return
        key = str(path)
        if key in seen:
            return
        seen.add(key)
        if _isdir(path):
            homes.append(path)

    for item in extra or []:
        _add(item)

    if os.environ.get("SUDO_USER"):
        try:
            _add(pwd.getpwnam(os.environ["SUDO_USER"]).pw_dir)
        except Exception:
            pass

    for base in ("/home", "/Users"):
        for name in _listdir(Path(base)):
            _add(Path(base) / name)

    _add(Path.home())

    # Windows dev box / windows-app deployment.
    if os.name == "nt":
        for key in ("USERPROFILE", "HOME"):
            if os.environ.get(key):
                _add(os.environ[key])
    return homes


def discover_profiles(extra_homes: Optional[Iterable[str]] = None) -> List[Dict]:
    """Locate every readable history DB. Each entry is one browser profile."""
    found: List[Dict] = []
    for home in candidate_homes(extra_homes):
        owner = home.name
        for rel, browser in CHROMIUM_ROOTS + (WINDOWS_CHROMIUM_ROOTS if os.name == "nt" else []):
            base = home / rel
            if not _isdir(base):
                continue
            for profile_dir in _chromium_profile_dirs(base):
                db = profile_dir / "History"
                if _isfile(db):
                    found.append({
                        "browser": browser,
                        "family": "chromium",
                        "profile": f"{owner}:{profile_dir.name}",
                        "path": db,
                    })
        for rel, browser in FIREFOX_ROOTS:
            base = home / rel
            if not _isdir(base):
                continue
            for name in _listdir(base):
                db = base / name / "places.sqlite"
                if _isfile(db):
                    found.append({
                        "browser": browser,
                        "family": "firefox",
                        "profile": f"{owner}:{name}",
                        "path": db,
                    })
    return found


def _chromium_profile_dirs(base: Path) -> List[Path]:
    dirs: List[Path] = []
    if _isfile(base / "History"):
        dirs.append(base)
    for name in _listdir(base):
        if name == "Default" or name.startswith("Profile "):
            p = base / name
            if _isdir(p):
                dirs.append(p)
    return dirs


# ─── safe reading ───────────────────────────────────────────────────────────

class _Snapshot:
    """Context manager giving a private, writable copy of a browser DB.

    The copy is writable on purpose: SQLite needs write access to replay a -wal
    file, and Chromium switched its history DB to WAL mode. Opening the original
    read-only-immutable would silently hide the most recent visits — exactly the
    rows that matter most here.
    """

    def __init__(self, src: Path):
        self.src = src
        self.tmpdir: Optional[str] = None
        self.copy: Optional[Path] = None

    def __enter__(self) -> Optional[Path]:
        try:
            self.tmpdir = tempfile.mkdtemp(prefix="gs-hist-")
            dest = Path(self.tmpdir) / self.src.name
            shutil.copy2(self.src, dest)
            for sidecar in ("-wal", "-shm"):
                side = Path(str(self.src) + sidecar)
                if side.exists():
                    try:
                        shutil.copy2(side, Path(str(dest) + sidecar))
                    except Exception:
                        pass
            self.copy = dest
            return dest
        except PermissionError:
            logger.debug("No permission to read %s", self.src)
            return None
        except Exception as exc:
            logger.debug("Could not snapshot %s: %s", self.src, exc)
            return None

    def __exit__(self, *_exc) -> None:
        if self.tmpdir:
            shutil.rmtree(self.tmpdir, ignore_errors=True)


def _connect(db_path: Path) -> Optional[sqlite3.Connection]:
    for uri in (
        f"file:{db_path.as_posix()}",
        f"file:{db_path.as_posix()}?mode=ro&immutable=1",
    ):
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT 1")
            return conn
        except Exception:
            continue
    return None


def _read_chromium(conn: sqlite3.Connection, since_epoch: float) -> List[Dict]:
    since_chrome = int((since_epoch + CHROME_EPOCH_OFFSET_SEC) * 1_000_000)
    rows = conn.execute(
        """
        SELECT v.id AS vid, v.visit_time AS vt, v.visit_duration AS vd,
               u.url AS url, u.title AS title
          FROM visits v JOIN urls u ON u.id = v.url
         WHERE v.visit_time > ?
         ORDER BY v.visit_time ASC
         LIMIT ?
        """,
        (since_chrome, MAX_ROWS_PER_PROFILE),
    ).fetchall()
    out: List[Dict] = []
    for r in rows:
        try:
            ts = (int(r["vt"]) / 1_000_000.0) - CHROME_EPOCH_OFFSET_SEC
        except Exception:
            continue
        if ts <= 0:
            continue
        raw_dwell = 0
        try:
            raw_dwell = int(r["vd"] or 0) // 1_000_000
        except Exception:
            raw_dwell = 0
        out.append({"vid": r["vid"], "ts": ts, "url": r["url"],
                    "title": r["title"], "browser_dwell": max(0, raw_dwell)})
    return out


def _read_firefox(conn: sqlite3.Connection, since_epoch: float) -> List[Dict]:
    since_us = int(since_epoch * 1_000_000)
    rows = conn.execute(
        """
        SELECT hv.id AS vid, hv.visit_date AS vt, p.url AS url, p.title AS title
          FROM moz_historyvisits hv JOIN moz_places p ON p.id = hv.place_id
         WHERE hv.visit_date > ?
         ORDER BY hv.visit_date ASC
         LIMIT ?
        """,
        (since_us, MAX_ROWS_PER_PROFILE),
    ).fetchall()
    out: List[Dict] = []
    for r in rows:
        try:
            ts = int(r["vt"]) / 1_000_000.0
        except Exception:
            continue
        if ts <= 0:
            continue
        out.append({"vid": r["vid"], "ts": ts, "url": r["url"],
                    "title": r["title"], "browser_dwell": 0})
    return out


# ─── dwell inference ────────────────────────────────────────────────────────

def build_records(profile: Dict, rows: List[Dict], now: Optional[float] = None
                  ) -> Tuple[List[Dict], float]:
    """Turn raw visit rows into upload records, and return the new watermark.

    Dwell for a visit is the distance to the next visit in the same profile,
    capped. The final row is withheld (watermark stays behind it) until either a
    successor appears or the settle window proves browsing has stopped — see the
    module docstring for why emitting it early is unfixable.
    """
    now = time.time() if now is None else now
    records: List[Dict] = []
    watermark = 0.0
    if not rows:
        return records, watermark

    rows = sorted(rows, key=lambda r: r["ts"])
    for i, row in enumerate(rows):
        is_last = i == len(rows) - 1
        if is_last:
            if (now - row["ts"]) < TAIL_SETTLE_SEC:
                break  # hold it back; watermark stays below this visit
            dwell = min(row.get("browser_dwell") or TAIL_DWELL_SEC, DWELL_CAP_SEC)
        else:
            gap = int(max(0, rows[i + 1]["ts"] - row["ts"]))
            measured = row.get("browser_dwell") or 0
            # Chromium reports real foreground duration; trust it when present,
            # but never let it exceed the gap to the next visit.
            dwell = min(measured, gap) if measured > 0 else gap
            dwell = min(dwell, DWELL_CAP_SEC)

        info = parse_url(row.get("url") or "")
        if not info:
            watermark = max(watermark, row["ts"])
            continue

        visit_key = f"{profile['browser']}|{profile['profile']}|{row['vid']}|{int(row['ts'])}"
        records.append({
            "kind": "web",
            "visit_key": visit_key[:180],
            "browser": profile["browser"],
            "domain": info["domain"],
            "url_path": info["url_path"],
            "title": clean_title(row.get("title"), info["platform"]),
            "visited_at": utc_iso(row["ts"]),
            "day": local_day(row["ts"]),
            "dwell_sec": int(dwell),
            "platform": info["platform"],
            "media_id": info["media_id"],
            "media_kind": info["media_kind"],
        })
        watermark = max(watermark, row["ts"])
    return records, watermark


# ─── collector ──────────────────────────────────────────────────────────────

class BrowserHistoryCollector:
    """Polls every discovered browser profile and spools new visits."""

    def __init__(
        self,
        spool: Spool,
        state_path: Path | str,
        extra_homes: Optional[Iterable[str]] = None,
        poll_interval: float = POLL_INTERVAL_SEC,
        first_run_lookback_days: int = 7,
    ):
        self.spool = spool
        self.state_path = Path(state_path)
        self.extra_homes = list(extra_homes or [])
        self.poll_interval = float(poll_interval)
        self.first_run_lookback_days = int(first_run_lookback_days)
        self._state: Dict[str, float] = self._load_state()
        self.stats = {
            "profiles_seen": 0,
            "visits_spooled": 0,
            "youtube_spooled": 0,
            "polls": 0,
            "errors": 0,
            "last_error": "",
        }

    # ─── watermark persistence ──────────────────────────────────────────────

    def _load_state(self) -> Dict[str, float]:
        try:
            if self.state_path.is_file():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {str(k): float(v) for k, v in data.get("watermarks", {}).items()}
        except Exception as exc:
            logger.warning("History watermark file unreadable, starting fresh: %s", exc)
        return {}

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"updated_at": utc_iso(), "watermarks": self._state}
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except Exception as exc:
            logger.warning("Could not persist history watermarks: %s", exc)

    def _watermark_for(self, profile: Dict) -> float:
        key = f"{profile['browser']}|{profile['profile']}"
        if key in self._state:
            return self._state[key]
        # First sight of a profile: look back a bounded window so the first
        # progress report is not empty, but do not import years of history.
        return time.time() - (self.first_run_lookback_days * 86400)

    # ─── one pass ───────────────────────────────────────────────────────────

    def collect_once(self, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        self.stats["polls"] += 1
        profiles = discover_profiles(self.extra_homes)
        self.stats["profiles_seen"] = len(profiles)
        if not profiles:
            logger.debug("No browser profiles found (searched %d home dirs)",
                         len(candidate_homes(self.extra_homes)))
            return 0

        spooled = 0
        for profile in profiles:
            try:
                spooled += self._collect_profile(profile, now)
            except Exception as exc:
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{profile.get('profile')}: {exc}"
                logger.debug("History read failed for %s: %s", profile.get("profile"), exc)
        if spooled:
            self._save_state()
        return spooled

    def _collect_profile(self, profile: Dict, now: float) -> int:
        since = self._watermark_for(profile)
        with _Snapshot(profile["path"]) as copy:
            if not copy:
                return 0
            conn = _connect(copy)
            if conn is None:
                return 0
            try:
                if profile["family"] == "chromium":
                    rows = _read_chromium(conn, since)
                else:
                    rows = _read_firefox(conn, since)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        records, watermark = build_records(profile, rows, now=now)
        key = f"{profile['browser']}|{profile['profile']}"
        if watermark > 0:
            self._state[key] = watermark
        elif key not in self._state:
            # Nothing readable yet, but remember we have seen the profile so the
            # lookback window is not re-applied on every poll.
            self._state[key] = since

        if not records:
            return 0
        written = self.spool.extend(records)
        self.stats["visits_spooled"] += written
        self.stats["youtube_spooled"] += sum(
            1 for r in records if r.get("platform") == "youtube"
        )
        logger.info("History: %d new visit(s) from %s (%s)",
                    written, profile["browser"], profile["profile"])
        return written

    # ─── lifecycle ──────────────────────────────────────────────────────────

    def run_loop(self, is_running) -> None:
        """Daemon-thread body. Never raises."""
        logger.info("Browser history collector started (poll %.0fs)", self.poll_interval)
        # Small stagger so the first pass does not land on top of agent startup.
        time.sleep(20)
        while True:
            try:
                if not is_running():
                    break
                self.collect_once()
            except Exception as exc:
                logger.debug("History collector tick note: %s", exc)
            time.sleep(self.poll_interval)
        logger.info("Browser history collector stopped (%s)", self.stats)

