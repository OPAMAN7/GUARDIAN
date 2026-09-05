#!/usr/bin/env python3
"""
Guardian Agent — Offline Spool
==============================

A tiny durable append-only queue for telemetry that must survive a Wi-Fi drop,
a server restart or an agent restart without ever being counted twice.

Why this exists
---------------
The academic-progress collectors produce facts the parent is going to be shown
as *numbers* (minutes of study, a grade). Dropping records makes the grade too
low; re-sending records makes it too high. Neither is acceptable, so records go
to disk first and are only removed once the server has acknowledged them.

Design
------
* One JSON object per line (``.jsonl``). A truncated final line — the classic
  power-loss artefact — costs exactly one record and never breaks the file.
* ``append()`` is O(1) and holds the lock for a single write.
* ``drain()`` *rotates* the file under the lock, then talks to the network with
  the lock released, then re-queues whatever was not accepted. A collector
  thread is therefore never blocked by a slow or hanging upload.
* Every record carries a caller-supplied idempotency key. The server side uses
  ``INSERT OR IGNORE`` against a UNIQUE index, so a replay after a half-failed
  send is a no-op rather than a double count.
* The file is hard-capped. A Pi that has been offline for a month must not fill
  its SD card; the *oldest* records are dropped first because recent activity
  matters more for a progress report.

This module deliberately has no imports beyond the standard library and knows
nothing about sockets — it takes a ``sender`` callable.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# A drained batch has to fit inside the server's socket buffer (10 MB) with room
# to spare; 250 usage/visit records is roughly 60-90 KB of JSON.
DEFAULT_BATCH_SIZE = 250

# Hard ceilings. Whichever trips first wins.
DEFAULT_MAX_RECORDS = 40_000
DEFAULT_MAX_BYTES = 12 * 1024 * 1024  # 12 MB


class Spool:
    """Durable JSONL queue with at-least-once delivery and idempotent replay."""

    def __init__(
        self,
        path: Path | str,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        self.path = Path(path)
        self.max_records = int(max_records)
        self.max_bytes = int(max_bytes)
        self._lock = threading.Lock()
        self._dropped_total = 0
        self._sent_total = 0
        # Records currently on disk. ``None`` means "not counted yet" — the file
        # is read once, lazily, so a restart picks up an existing queue without
        # paying for a full scan on every append.
        self._count: Optional[int] = None

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover - unwritable dir
            logger.warning("Spool directory not writable (%s): %s", self.path.parent, exc)

    # ─── writing ────────────────────────────────────────────────────────────

    def append(self, record: Dict) -> bool:
        """Queue one record. Returns False only if the disk write failed."""
        return self.extend([record]) == 1

    def extend(self, records: Iterable[Dict]) -> int:
        """Queue many records in one write. Returns how many were persisted."""
        lines: List[str] = []
        for rec in records:
            try:
                lines.append(json.dumps(rec, separators=(",", ":"), default=str))
            except (TypeError, ValueError) as exc:
                logger.warning("Spool skipped unserialisable record: %s", exc)
        if not lines:
            return 0

        blob = "\n".join(lines) + "\n"
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(blob)
                    fh.flush()
                    os.fsync(fh.fileno())
            except Exception as exc:
                logger.error("Spool append failed (%s): %s", self.path, exc)
                return 0
            if self._count is None:
                # First write of this process: adopt whatever was already queued
                # (the count below therefore already includes ``lines``).
                self._count = self._count_lines_locked()
            else:
                self._count += len(lines)
            self._trim_locked()
        return len(lines)

    def _count_lines_locked(self) -> int:
        try:
            with self.path.open("rb") as fh:
                return sum(1 for line in fh if line.strip())
        except Exception:
            return 0

    def _trim_locked(self) -> None:
        """Enforce the ceilings by discarding the OLDEST records."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if (self._count is not None
                and self._count <= self.max_records
                and size <= self.max_bytes):
            return  # cheap path: no read, no rewrite

        try:
            kept = [r for r in self.path.read_text(
                encoding="utf-8", errors="replace").splitlines() if r.strip()]
        except Exception:
            return
        before = len(kept)

        over_records = max(0, before - self.max_records)
        if over_records:
            kept = kept[over_records:]

        # Trim from the front until the byte budget is met. Line sizes are
        # measured once and consumed as a running total: popping one line at a
        # time and re-encoding the whole list is quadratic, and on a full 12 MB
        # spool that is minutes of CPU on a Pi.
        sizes = [len(r.encode("utf-8")) + 1 for r in kept]
        total = sum(sizes)
        drop = 0
        while drop < len(kept) and total > self.max_bytes:
            total -= sizes[drop]
            drop += 1
        if drop:
            kept = kept[drop:]

        dropped = before - len(kept)
        if dropped <= 0:
            self._count = len(kept)
            return

        self._dropped_total += dropped
        logger.warning(
            "Spool over capacity — dropped %d oldest record(s) from %s (total dropped %d)",
            dropped, self.path.name, self._dropped_total,
        )
        self._atomic_write_locked(("\n".join(kept) + "\n") if kept else "")

    def _atomic_write_locked(self, text: str) -> None:
        """Replace the spool file without ever leaving it half-written."""
        try:
            fd, tmp = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
                self._count = sum(1 for ln in text.splitlines() if ln.strip())
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:
            self._count = None  # disk state now unknown; recount on next append
            logger.error("Spool rewrite failed (%s): %s", self.path, exc)

    # ─── reading / draining ─────────────────────────────────────────────────

    def pending(self) -> int:
        """Number of queued records (including any mid-flight rotation file)."""
        total = 0
        for p in (self.path, self._sending_path()):
            try:
                if p.exists():
                    with p.open("r", encoding="utf-8", errors="replace") as fh:
                        total += sum(1 for line in fh if line.strip())
            except Exception:
                continue
        return total

    def _sending_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".sending")

    def drain(
        self,
        sender: Callable[[List[Dict]], Optional[int]],
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_batches: int = 40,
    ) -> int:
        """Ship queued records through ``sender``.

        ``sender(batch)`` must return the number of records the server accepted,
        or ``None`` / raise to signal "not delivered, keep them". A batch is only
        discarded on a positive acknowledgement, so the failure mode is a
        duplicate send (which the server ignores) and never a silent loss.

        Returns the number of records handed off successfully.
        """
        staged = self._rotate_for_send()
        if not staged:
            return 0

        records, bad_lines = _parse_lines(staged)
        if bad_lines:
            logger.warning("Spool skipped %d unparseable line(s)", bad_lines)

        shipped = 0
        idx = 0
        batches = 0
        while idx < len(records) and batches < max_batches:
            batch = records[idx: idx + batch_size]
            batches += 1
            try:
                accepted = sender(batch)
            except Exception as exc:
                logger.warning("Spool sender raised, re-queueing %d record(s): %s",
                               len(records) - idx, exc)
                break
            if accepted is None:
                logger.debug("Spool sender declined, re-queueing %d record(s)",
                             len(records) - idx)
                break
            idx += len(batch)
            shipped += len(batch)
            self._sent_total += len(batch)

        leftover = records[idx:]
        self._finish_send(leftover)
        if shipped:
            logger.info("Spool shipped %d record(s) from %s (%d left)",
                        shipped, self.path.name, len(leftover))
        return shipped

    def _rotate_for_send(self) -> List[str]:
        """Move the queue aside so appends keep working during the upload."""
        sending = self._sending_path()
        with self._lock:
            # Recover an orphaned rotation file first (previous crash mid-send).
            existing: List[str] = []
            if sending.exists():
                try:
                    existing = sending.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                except Exception:
                    existing = []
            if self.path.exists():
                try:
                    fresh = self.path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                except Exception:
                    fresh = []
                if fresh:
                    existing.extend(fresh)
                try:
                    self.path.unlink()
                except OSError:
                    pass
            rows = [r for r in existing if r.strip()]
            if not rows:
                try:
                    if sending.exists():
                        sending.unlink()
                except OSError:
                    pass
                self._count = 0
                return []
            try:
                sending.write_text("\n".join(rows) + "\n", encoding="utf-8")
            except Exception as exc:
                logger.error("Spool rotation failed: %s", exc)
                # Put them back so nothing is lost.
                self._atomic_write_locked("\n".join(rows) + "\n")
                return []
            self._count = 0  # the live queue is now empty; the batch is staged
            return rows

    def _finish_send(self, leftover: List[Dict]) -> None:
        """Drop the rotation file and put anything undelivered back in front."""
        sending = self._sending_path()
        with self._lock:
            try:
                if sending.exists():
                    sending.unlink()
            except OSError:
                pass
            if not leftover:
                return
            try:
                tail = ""
                if self.path.exists():
                    tail = self.path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                tail = ""
            head = "\n".join(
                json.dumps(r, separators=(",", ":"), default=str) for r in leftover
            )
            self._atomic_write_locked(head + "\n" + tail)
            self._trim_locked()

    # ─── introspection (used by diagnostics) ────────────────────────────────

    def stats(self) -> Dict[str, int]:
        return {
            "pending": self.pending(),
            "sent_total": self._sent_total,
            "dropped_total": self._dropped_total,
            "bytes": self.path.stat().st_size if self.path.exists() else 0,
        }


def _parse_lines(lines: List[str]):
    records: List[Dict] = []
    bad = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            bad += 1
            continue
        if isinstance(obj, dict):
            records.append(obj)
        else:
            bad += 1
    return records, bad


def utc_iso(ts: Optional[float] = None) -> str:
    """UTC timestamp in the exact format SQLite's datetime() comparisons expect."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts if ts is not None else time.time()))


def local_day(ts: Optional[float] = None) -> str:
    """Local calendar date of ``ts``.

    The child's day boundary is *their* midnight, not UTC's — a 23:30 study
    session belongs to that evening. The Pi's timezone is the child's timezone,
    so the agent is the only place this can be computed correctly.
    """
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))
