#!/usr/bin/env python3
"""
Offline wiring test for the academic-progress uplink inside main.py.

Run from the repo root:
    python pi-agent/tests/test_agent_wiring.py

The agent's real main.py is loaded, not a copy — its third-party and hardware
dependencies (socketio, psutil, mss, the blocker, the screenshotter) are replaced
with stubs in sys.modules first, while spool.py, usage_tracker.py and
browser_history.py load for real. That makes it possible to assert the wiring
that no unit test of the collectors can reach: that the threads are started, that
a batch is only dropped when the server acknowledges it, and that a partial
deploy degrades the progress feature instead of killing the agent.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

AGENT_DIR = Path(__file__).resolve().parents[1] / "agent"

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


def _stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


def install_stubs() -> None:
    """Replace everything main.py imports except the three progress modules."""
    _stub("socketio", Client=MagicMock, exceptions=types.SimpleNamespace(
        TimeoutError=TimeoutError, BadNamespaceError=RuntimeError))
    _stub("psutil", cpu_percent=lambda *a, **k: 1.0, virtual_memory=MagicMock(),
          process_iter=lambda *a, **k: iter(()), Process=MagicMock,
          NoSuchProcess=Exception, AccessDenied=Exception)
    _stub("screenshot", ScreenshotCapture=MagicMock)
    _stub("blocker", WebsiteBlocker=MagicMock, BlocklistUpdater=MagicMock)
    _stub("download_guard", DownloadGuard=MagicMock)
    _stub("cache", ScreenshotCache=MagicMock, CacheUploader=MagicMock)
    _stub("consent", check_stealth_consent=lambda: False,
          request_stealth_consent=lambda: False)
    _stub("guardian_watchdog", write_pid=lambda *a, **k: None,
          AGENT_PID_FILE="/tmp/x.pid",
          start_watchdog_subprocess=lambda *a, **k: None,
          _ensure_pid_dir=lambda *a, **k: None)
    _stub("block_page_server", start_block_server=lambda *a, **k: None)
    _stub("buzzer", play_warning=lambda *a, **k: None)
    _stub("app_tracker", AppTracker=MagicMock)


def load_main(tmp: Path):
    sys.path.insert(0, str(AGENT_DIR))
    install_stubs()
    spec = importlib.util.spec_from_file_location("guardian_main", AGENT_DIR / "main.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["guardian_main"] = mod
    spec.loader.exec_module(mod)
    mod.PROGRESS_STATE_DIR = tmp / "progress"
    return mod


class FakeSio:
    """Stands in for socketio.Client, recording every ack round-trip."""

    def __init__(self):
        self.calls = []
        self.behaviour = lambda event, payload: {"ok": True,
                                                 "accepted": len(payload["records"])}
        self.connected = True

    def call(self, event, payload, timeout=None):
        self.calls.append((event, payload, timeout))
        result = self.behaviour(event, payload)
        if isinstance(result, Exception):
            raise result
        return result


def build_agent(mod, **overrides):
    cfg = {"device_name": "aman-test", "server_url": "http://192.168.1.10:3000",
           "auth_token": "t", "enable_download_guard": False,
           "enable_block_page_server": False, "use_online_blocklist": False}
    cfg.update(overrides)
    agent = mod.GuardianAgent(cfg)
    agent.server = MagicMock()
    agent.server.device_name = "aman-test"
    agent.server.connected = True
    agent.server.sio = FakeSio()
    return agent


def test_wiring(mod, tmp: Path) -> None:
    print("\nAgent wiring")
    agent = build_agent(mod)
    check("usage collector constructed", agent.usage_tracker is not None)
    check("history collector constructed", agent.history_collector is not None)
    check("two separate spool files",
          agent.usage_spool is not None and agent.web_spool is not None
          and agent.usage_spool.path != agent.web_spool.path,
          f"{getattr(agent.usage_spool, 'path', None)} / {getattr(agent.web_spool, 'path', None)}")
    check("spools live under the progress state dir",
          "progress" in str(agent.usage_spool.path),
          f"got {agent.usage_spool.path}")
    check("usage collector reads through the shared AppTracker",
          agent.usage_tracker.app_tracker is agent.app_tracker)

    print("\nDegraded modes")
    off = build_agent(mod, enable_progress_tracking=False)
    check("config switch disables collection",
          off.usage_tracker is None and off.usage_spool is None)

    saved = mod.PROGRESS_IMPORT_ERROR
    mod.PROGRESS_IMPORT_ERROR = RuntimeError("simulated partial deploy")
    try:
        broken = build_agent(mod)
        ok = broken.usage_tracker is None and broken.history_collector is None
    except Exception as exc:
        ok = False
        print(f"        agent construction raised: {exc}")
    finally:
        mod.PROGRESS_IMPORT_ERROR = saved
    check("missing collector module degrades instead of killing the agent", ok)


def test_ack_contract(mod, tmp: Path) -> None:
    print("\nUplink ack contract")
    agent = build_agent(mod)
    sio = agent.server.sio

    for i in range(3):
        agent.usage_spool.append({"kind": "usage", "session_key": f"k{i}", "i": i})
    accepted = agent.usage_spool.drain(
        lambda b: agent._ship_progress("usage_batch", b))
    check("accepted batch leaves the spool",
          accepted == 3 and agent.usage_spool.pending() == 0,
          f"accepted={accepted} pending={agent.usage_spool.pending()}")
    check("emitted on the usage_batch event with a device name",
          sio.calls[-1][0] == "usage_batch"
          and sio.calls[-1][1]["device_name"] == "aman-test",
          f"got {sio.calls[-1][0]}")
    check("ack timeout is passed to socket.io",
          sio.calls[-1][2] == mod.PROGRESS_ACK_TIMEOUT_SEC, f"got {sio.calls[-1][2]}")

    # Server never answers.
    agent.usage_spool.append({"kind": "usage", "session_key": "late"})
    sio.behaviour = lambda e, p: TimeoutError("no ack")
    shipped = agent.usage_spool.drain(lambda b: agent._ship_progress("usage_batch", b))
    check("timeout requeues the batch",
          shipped == 0 and agent.usage_spool.pending() == 1,
          f"shipped={shipped} pending={agent.usage_spool.pending()}")

    # Server answers "not ok".
    sio.behaviour = lambda e, p: {"ok": False, "error": "unknown device"}
    shipped = agent.usage_spool.drain(lambda b: agent._ship_progress("usage_batch", b))
    check("explicit refusal keeps the records",
          shipped == 0 and agent.usage_spool.pending() == 1,
          f"shipped={shipped} pending={agent.usage_spool.pending()}")

    # Duplicates: the server accepts fewer than it was sent. That is success.
    sio.behaviour = lambda e, p: {"ok": True, "accepted": 0}
    shipped = agent.usage_spool.drain(lambda b: agent._ship_progress("usage_batch", b))
    check("de-duplicated batch is not retried forever",
          agent.usage_spool.pending() == 0,
          f"pending={agent.usage_spool.pending()}")

    # Records survive a "restart" — a fresh Spool over the same file.
    agent.usage_spool.append({"kind": "usage", "session_key": "persisted"})
    reborn = mod.Spool(agent.usage_spool.path)
    check("queue survives an agent restart", reborn.pending() == 1,
          f"got {reborn.pending()}")
    reborn.drain(lambda b: len(b))


def test_uplink_loop(mod, tmp: Path) -> None:
    print("\nUplink loop")
    agent = build_agent(mod)
    sio = agent.server.sio
    agent.usage_spool.append({"kind": "usage", "session_key": "u1"})
    agent.web_spool.append({"kind": "web", "visit_key": "w1"})

    # Make the loop's two sleeps instant without touching real time semantics.
    fake_time = types.SimpleNamespace(
        sleep=lambda s: time.sleep(0.001), time=time.time,
        gmtime=time.gmtime, localtime=time.localtime, strftime=time.strftime,
        mktime=time.mktime, monotonic=time.monotonic)
    real_time, mod.time = mod.time, fake_time
    try:
        agent._running = True
        import threading
        t = threading.Thread(target=agent._progress_uplink_loop, daemon=True)
        t.start()
        deadline = time.time() + 5.0
        while time.time() < deadline and len(sio.calls) < 2:
            time.sleep(0.02)
        agent._running = False
        t.join(timeout=2.0)
    finally:
        mod.time = real_time

    events = [c[0] for c in sio.calls]
    check("loop drains the app ledger", "usage_batch" in events, f"got {events[:4]}")
    check("loop drains the web ledger", "web_batch" in events, f"got {events[:4]}")
    check("the two ledgers are shipped separately, never merged",
          all(len({r["kind"] for r in c[1]["records"]}) == 1 for c in sio.calls),
          "a batch mixed app and web records")
    check("both spools emptied",
          agent.usage_spool.pending() == 0 and agent.web_spool.pending() == 0,
          f"usage={agent.usage_spool.pending()} web={agent.web_spool.pending()}")

    print("\nShutdown")
    agent2 = build_agent(mod)
    agent2.usage_tracker.app_tracker = MagicMock()
    agent2.usage_tracker.app_tracker.get_foreground_snapshot.return_value = {
        "app_name": "Thonny", "process_name": "thonny",
        "window_title": "essay.py", "platform": "linux"}
    t0 = time.time() - 100
    for k in range(0, 61, 5):
        agent2.usage_tracker.poll_once(now=t0 + k, idle_sec=None)
    before = agent2.usage_spool.pending()
    agent2.stop()
    check("stop() flushes the open session to disk",
          agent2.usage_spool.pending() == before + 1,
          f"{before} -> {agent2.usage_spool.pending()}")


def main() -> int:
    print("=" * 66)
    print("GUARDIAN academic-progress agent wiring — offline test suite")
    print("=" * 66)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mod = load_main(tmp)
        test_wiring(mod, tmp)
        test_ack_contract(mod, tmp)
        test_uplink_loop(mod, tmp)
    print("\n" + "=" * 66)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
