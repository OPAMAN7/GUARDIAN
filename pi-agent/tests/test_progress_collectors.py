#!/usr/bin/env python3
"""
Offline tests for the academic-progress collectors.

Run from the repo root:
    python pi-agent/tests/test_progress_collectors.py

No network, no browser and no Raspberry Pi required — the browser databases are
built from scratch with the real Chromium and Firefox schemas, and the usage
state machine is driven with an injected clock.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1] / "agent"
sys.path.insert(0, str(AGENT_DIR))

import browser_history as bh  # noqa: E402
import spool as sp  # noqa: E402
from usage_tracker import UsageTracker  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


# ─── spool ──────────────────────────────────────────────────────────────────

def test_spool(tmp: Path) -> None:
    print("\nSpool")
    s = sp.Spool(tmp / "q.jsonl")
    for i in range(7):
        s.append({"kind": "usage", "i": i})
    check("pending counts appended records", s.pending() == 7, f"got {s.pending()}")

    seen = []
    shipped = s.drain(lambda batch: (seen.extend(batch), len(batch))[1], batch_size=3)
    check("drain ships everything on success", shipped == 7 and s.pending() == 0,
          f"shipped={shipped} pending={s.pending()}")
    check("order preserved", [r["i"] for r in seen] == list(range(7)))

    # Sender refuses -> nothing is lost.
    for i in range(4):
        s.append({"i": 100 + i})
    shipped = s.drain(lambda batch: None)
    check("declined batch is re-queued", shipped == 0 and s.pending() == 4,
          f"shipped={shipped} pending={s.pending()}")

    # Sender raises mid-way -> only the acknowledged prefix is dropped.
    calls = {"n": 0}

    def flaky(batch):
        calls["n"] += 1
        if calls["n"] == 1:
            return len(batch)
        raise RuntimeError("socket died")

    shipped = s.drain(flaky, batch_size=2)
    check("partial failure keeps the remainder", shipped == 2 and s.pending() == 2,
          f"shipped={shipped} pending={s.pending()}")
    rest = []
    s.drain(lambda b: (rest.extend(b), len(b))[1])
    check("remainder is the correct records", [r["i"] for r in rest] == [102, 103],
          f"got {[r.get('i') for r in rest]}")

    # A truncated final line costs one record, not the file.
    s2 = sp.Spool(tmp / "torn.jsonl")
    s2.append({"i": 1})
    s2.append({"i": 2})
    with (tmp / "torn.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"i": 3, "hal')
    got = []
    s2.drain(lambda b: (got.extend(b), len(b))[1])
    check("torn line skipped, good records kept", [r["i"] for r in got] == [1, 2],
          f"got {[r.get('i') for r in got]}")

    # Appends during a drain are not lost.
    s3 = sp.Spool(tmp / "live.jsonl")
    for i in range(3):
        s3.append({"i": i})

    def sender_that_appends(batch):
        s3.append({"i": 99})
        return len(batch)

    s3.drain(sender_that_appends, batch_size=3)
    check("record appended during drain survives", s3.pending() == 1,
          f"pending={s3.pending()}")

    # Capacity ceiling drops the OLDEST first.
    s4 = sp.Spool(tmp / "cap.jsonl", max_records=10)
    for i in range(25):
        s4.append({"i": i})
    kept = []
    s4.drain(lambda b: (kept.extend(b), len(b))[1])
    check("capacity cap keeps newest records",
          len(kept) == 10 and kept[0]["i"] == 15 and kept[-1]["i"] == 24,
          f"n={len(kept)} first={kept[0]['i'] if kept else None}")


# ─── usage tracker ──────────────────────────────────────────────────────────

class FakeTracker:
    def __init__(self):
        self.snap = None

    def get_foreground_snapshot(self):
        return self.snap


def _snap(proc, name=None, title=""):
    return {"app_name": name or proc.title(), "process_name": proc,
            "window_title": title, "platform": "linux"}


def _drain(s):
    out = []
    s.drain(lambda b: (out.extend(b), len(b))[1])
    return out


def test_usage_sessions(tmp: Path) -> None:
    print("\nUsage tracker — session collapsing")
    s = sp.Spool(tmp / "u1.jsonl")
    ft = FakeTracker()
    ut = UsageTracker(ft, s, idle_detection=False)

    t0 = time.mktime((2026, 9, 4, 10, 0, 0, 0, 0, -1))
    ft.snap = _snap("thonny", "Thonny", "hello.py")
    for k in range(0, 121, 5):          # 120 s of Thonny
        ut.poll_once(now=t0 + k, idle_sec=None)
    ft.snap = _snap("chromium", "Chromium", "Instagram")
    for k in range(125, 246, 5):        # then 120 s of a browser
        ut.poll_once(now=t0 + k, idle_sec=None)
    ut.flush()

    recs = _drain(s)
    check("one record per focused app", len(recs) == 2, f"got {len(recs)}")
    by_proc = {r["process_name"]: r for r in recs}
    check("thonny session length is right",
          115 <= by_proc["thonny"]["duration_sec"] <= 125,
          f"got {by_proc.get('thonny', {}).get('duration_sec')}")
    check("browser flagged is_browser=1", by_proc["chromium"]["is_browser"] == 1)
    check("non-browser flagged is_browser=0", by_proc["thonny"]["is_browser"] == 0)
    check("browser window title dropped (belongs to the web ledger)",
          by_proc["chromium"]["window_title"] is None,
          f"got {by_proc['chromium']['window_title']!r}")
    check("non-browser keeps its window title",
          by_proc["thonny"]["window_title"] == "hello.py")
    check("session_key is stable and non-empty",
          len(by_proc["thonny"]["session_key"]) == 24)

    print("\nUsage tracker — flicker, gaps, idle, midnight")
    s = sp.Spool(tmp / "u2.jsonl")
    ut = UsageTracker(FakeTracker(), s, idle_detection=False)
    ut.app_tracker.snap = _snap("gedit")
    ut.poll_once(now=t0, idle_sec=None)
    ut.app_tracker.snap = _snap("firefox")
    ut.poll_once(now=t0 + 2, idle_sec=None)   # 2 s of gedit -> below floor
    ut.flush()
    recs = _drain(s)
    check("sub-5s flicker is discarded",
          all(r["process_name"] != "gedit" for r in recs), f"got {recs}")

    s = sp.Spool(tmp / "u3.jsonl")
    ut = UsageTracker(FakeTracker(), s, idle_detection=False)
    ut.app_tracker.snap = _snap("libreoffice")
    for k in range(0, 61, 5):            # 60 s observed at the real cadence
        ut.poll_once(now=t0 + k, idle_sec=None)
    # Agent suspended for an hour, then wakes up on the same app.
    ut.poll_once(now=t0 + 3660, idle_sec=None)
    ut.flush()
    recs = _drain(s)
    check("unobserved gap is not credited",
          len(recs) >= 1 and recs[0]["duration_sec"] == 60,
          f"got {[r['duration_sec'] for r in recs]}")
    check("gap close is recorded as such",
          recs and recs[0]["close_reason"] == "gap",
          f"got {recs[0]['close_reason'] if recs else None}")

    s = sp.Spool(tmp / "u4.jsonl")
    ut = UsageTracker(FakeTracker(), s, idle_detection=True, idle_threshold_sec=300)
    ut.app_tracker.snap = _snap("vlc")
    # Active for 5 minutes, then walks away: xprintidle climbs from 0.
    for k in range(0, 621, 20):
        ut.poll_once(now=t0 + k, idle_sec=0 if k <= 300 else (k - 300))
    ut.flush()
    recs = _drain(s)
    check("idle time is trimmed off the session",
          len(recs) == 1 and recs[0]["duration_sec"] == 300,
          f"got {[r['duration_sec'] for r in recs]}")
    check("idle close is recorded as such",
          recs and recs[0]["close_reason"] == "idle",
          f"got {recs[0]['close_reason'] if recs else None}")

    s = sp.Spool(tmp / "u5.jsonl")
    ut = UsageTracker(FakeTracker(), s, idle_detection=False, poll_interval=60)
    late = time.mktime((2026, 9, 4, 23, 55, 0, 0, 0, -1))
    ut.app_tracker.snap = _snap("anki")
    for k in range(0, 1500, 60):     # 23:55 -> 00:20 next day
        ut.poll_once(now=late + k, idle_sec=None)
    ut.flush()
    recs = _drain(s)
    days = sorted({r["day"] for r in recs})
    check("session split at local midnight", len(recs) == 2 and len(days) == 2,
          f"records={len(recs)} days={days}")
    check("split days are consecutive", days == ["2026-09-04", "2026-09-05"],
          f"got {days}")

    s = sp.Spool(tmp / "u6.jsonl")
    ut = UsageTracker(FakeTracker(), s, idle_detection=False)
    ut.app_tracker.snap = _snap("desktop")     # nothing focused
    for k in range(0, 300, 5):
        ut.poll_once(now=t0 + k, idle_sec=None)
    ut.flush()
    check("idle desktop produces no session", _drain(s) == [])


# ─── url parsing ────────────────────────────────────────────────────────────

def test_url_parsing() -> None:
    print("\nURL parsing and YouTube extraction")
    cases = [
        ("https://www.khanacademy.org/math/algebra?utm=abc",
         {"domain": "khanacademy.org", "url_path": "/math/algebra",
          "platform": "web", "media_id": None}),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
         {"domain": "youtube.com", "platform": "youtube",
          "media_id": "dQw4w9WgXcQ", "media_kind": "video"}),
        ("https://m.youtube.com/shorts/AbCdEf12345",
         {"domain": "youtube.com", "platform": "youtube",
          "media_id": "AbCdEf12345", "media_kind": "short"}),
        ("https://youtu.be/xyz123ABCde",
         {"domain": "youtu.be", "platform": "youtube",
          "media_id": "xyz123ABCde", "media_kind": "video"}),
        ("https://www.youtube.com/live/LiVeStRm001",
         {"platform": "youtube", "media_id": "LiVeStRm001", "media_kind": "live"}),
        ("https://www.youtube.com/feed/subscriptions",
         {"platform": "youtube", "media_id": None, "media_kind": None}),
    ]
    for url, expected in cases:
        got = bh.parse_url(url)
        ok = got is not None and all(got.get(k) == v for k, v in expected.items())
        check(f"parse {url[:52]}", ok, f"got {got}")

    check("query string is never stored",
          "utm" not in json.dumps(bh.parse_url("https://a.com/x?utm=secret_search_term")))
    for bad in ("chrome://settings", "about:blank", "file:///home/pi/x.pdf",
                "http://localhost:3000/", ""):
        check(f"rejected {bad or '(empty)'}", bh.parse_url(bad) is None)
    check("YouTube suffix stripped from title",
          bh.clean_title("Integration by parts - YouTube") == "Integration by parts")


# ─── browser databases ──────────────────────────────────────────────────────

def make_chromium_db(path: Path, visits) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE urls (id INTEGER PRIMARY KEY, url LONGVARCHAR, title LONGVARCHAR,
                           visit_count INTEGER DEFAULT 0, typed_count INTEGER DEFAULT 0,
                           last_visit_time INTEGER, hidden INTEGER DEFAULT 0);
        CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER NOT NULL,
                             visit_time INTEGER NOT NULL, from_visit INTEGER,
                             transition INTEGER DEFAULT 0, segment_id INTEGER,
                             visit_duration INTEGER DEFAULT 0);
        """
    )
    for i, (url, title, ts, dur) in enumerate(visits, start=1):
        chrome_ts = int((ts + bh.CHROME_EPOCH_OFFSET_SEC) * 1_000_000)
        conn.execute("INSERT INTO urls (id, url, title, last_visit_time) VALUES (?,?,?,?)",
                     (i, url, title, chrome_ts))
        conn.execute(
            "INSERT INTO visits (id, url, visit_time, visit_duration) VALUES (?,?,?,?)",
            (i, i, chrome_ts, int(dur * 1_000_000)))
    conn.commit()
    conn.close()


def make_firefox_db(path: Path, visits) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url LONGVARCHAR,
                                 title LONGVARCHAR, visit_count INTEGER DEFAULT 0);
        CREATE TABLE moz_historyvisits (id INTEGER PRIMARY KEY, from_visit INTEGER,
                                        place_id INTEGER, visit_date INTEGER,
                                        visit_type INTEGER, session INTEGER);
        """
    )
    for i, (url, title, ts) in enumerate(visits, start=1):
        conn.execute("INSERT INTO moz_places (id, url, title) VALUES (?,?,?)", (i, url, title))
        conn.execute(
            "INSERT INTO moz_historyvisits (id, place_id, visit_date) VALUES (?,?,?)",
            (i, i, int(ts * 1_000_000)))
    conn.commit()
    conn.close()


def test_history_reading(tmp: Path) -> None:
    print("\nBrowser history reading")
    now = time.time()
    base = now - 7200          # two hours ago, well past the settle window
    cdb = tmp / "History"
    make_chromium_db(cdb, [
        ("https://www.khanacademy.org/math", "Algebra | Khan Academy", base, 0),
        ("https://www.youtube.com/watch?v=LEC0000001", "Calculus lecture 1 - YouTube",
         base + 600, 0),
        ("https://www.youtube.com/shorts/SHORT00001", "funny cat - YouTube", base + 1500, 0),
        ("https://stackoverflow.com/questions/1", "python list comp", base + 1600, 45),
        ("https://instagram.com/", "Instagram", base + 1700, 0),
    ])
    prof = {"browser": "chromium", "family": "chromium",
            "profile": "pi:Default", "path": cdb}
    with bh._Snapshot(cdb) as copy:
        conn = bh._connect(copy)
        rows = bh._read_chromium(conn, base - 3600)
        conn.close()
    check("chromium epoch decoded", len(rows) == 5 and abs(rows[0]["ts"] - base) < 2,
          f"n={len(rows)}")

    recs, watermark = bh.build_records(prof, rows, now=now)
    check("all five visits emitted (settle window passed)", len(recs) == 5, f"got {len(recs)}")
    kinds = {r["media_id"]: r for r in recs if r["media_id"]}
    check("youtube video captured", "LEC0000001" in kinds)
    check("short flagged as short",
          kinds.get("SHORT00001", {}).get("media_kind") == "short")
    dwells = {r["domain"]: r["dwell_sec"] for r in recs}
    check("dwell inferred from next visit", dwells["khanacademy.org"] == 600,
          f"got {dwells['khanacademy.org']}")
    check("browser-reported duration wins when smaller",
          dwells["stackoverflow.com"] == 45, f"got {dwells['stackoverflow.com']}")
    check("watermark advanced to newest visit", abs(watermark - (base + 1700)) < 2)
    check("visit_key is unique per visit",
          len({r["visit_key"] for r in recs}) == 5)

    # Newest visit inside the settle window must be held back.
    fresh = tmp / "History2"
    make_chromium_db(fresh, [
        ("https://a.org/1", "one", now - 4000, 0),
        ("https://a.org/2", "two", now - 60, 0),
    ])
    prof2 = dict(prof, path=fresh, profile="pi:P2")
    with bh._Snapshot(fresh) as copy:
        conn = bh._connect(copy)
        rows2 = bh._read_chromium(conn, 0)
        conn.close()
    recs2, wm2 = bh.build_records(prof2, rows2, now=now)
    check("newest visit held back until dwell is knowable", len(recs2) == 1,
          f"got {len(recs2)}")
    check("watermark stays behind the held-back visit", wm2 < (now - 100),
          f"wm={wm2} now={now}")

    fdb = tmp / "places.sqlite"
    make_firefox_db(fdb, [
        ("https://en.wikipedia.org/wiki/Algebra", "Algebra - Wikipedia", base),
        ("https://reddit.com/r/memes", "memes", base + 300),
    ])
    fprof = {"browser": "firefox", "family": "firefox",
             "profile": "pi:abc.default", "path": fdb}
    with bh._Snapshot(fdb) as copy:
        conn = bh._connect(copy)
        frows = bh._read_firefox(conn, 0)
        conn.close()
    frecs, _ = bh.build_records(fprof, frows, now=now)
    check("firefox epoch decoded and rows emitted", len(frecs) == 2, f"got {len(frecs)}")
    check("firefox domains parsed",
          {r["domain"] for r in frecs} == {"en.wikipedia.org", "reddit.com"},
          f"got {[r['domain'] for r in frecs]}")

    # The live file is never modified.
    before = cdb.stat().st_mtime, cdb.stat().st_size
    with bh._Snapshot(cdb) as copy:
        conn = bh._connect(copy)
        bh._read_chromium(conn, 0)
        conn.close()
    after = cdb.stat().st_mtime, cdb.stat().st_size
    check("original history file untouched", before == after, f"{before} -> {after}")


def test_watermarks(tmp: Path) -> None:
    print("\nWatermarks — no double counting across polls")
    now = time.time()
    base = now - 7200
    home = tmp / "home" / "pi"
    prof_dir = home / ".config" / "chromium" / "Default"
    prof_dir.mkdir(parents=True)
    make_chromium_db(prof_dir / "History", [
        ("https://khanacademy.org/a", "A", base, 0),
        ("https://khanacademy.org/b", "B", base + 100, 0),
        ("https://khanacademy.org/c", "C", base + 200, 0),
    ])
    s = sp.Spool(tmp / "web.jsonl")
    col = bh.BrowserHistoryCollector(s, tmp / "history_state.json",
                                    extra_homes=[str(home)])
    first = col.collect_once(now=now)
    check("first pass spools all settled visits", first == 3, f"got {first}")
    second = col.collect_once(now=now)
    check("second pass spools nothing new", second == 0, f"got {second}")

    make_chromium_db(prof_dir / "History2", [])   # unrelated file, ignored
    conn = sqlite3.connect(prof_dir / "History")
    chrome_ts = int((base + 900 + bh.CHROME_EPOCH_OFFSET_SEC) * 1_000_000)
    conn.execute("INSERT INTO urls (id,url,title,last_visit_time) VALUES (9,?,?,?)",
                 ("https://ncert.nic.in/x", "NCERT", chrome_ts))
    conn.execute("INSERT INTO visits (id,url,visit_time,visit_duration) VALUES (9,9,?,0)",
                 (chrome_ts,))
    conn.commit()
    conn.close()
    third = col.collect_once(now=now)
    check("only the genuinely new visit is spooled", third == 1, f"got {third}")

    saved = json.loads((tmp / "history_state.json").read_text())
    check("watermark persisted to disk", saved.get("watermarks"), f"got {saved}")

    fresh = bh.BrowserHistoryCollector(s, tmp / "history_state.json",
                                       extra_homes=[str(home)])
    check("restart re-reads nothing", fresh.collect_once(now=now) == 0)

    keys = [r["visit_key"] for r in _drain(s)]
    check("no duplicate visit keys across all polls", len(keys) == len(set(keys)),
          f"{len(keys)} keys, {len(set(keys))} unique")


def main() -> int:
    print("=" * 66)
    print("GUARDIAN academic-progress collectors — offline test suite")
    print("=" * 66)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_spool(tmp)
        test_usage_sessions(tmp)
        test_url_parsing()
        test_history_reading(tmp)
        test_watermarks(tmp)
    print("\n" + "=" * 66)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
