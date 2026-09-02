"""
Regression test for BUG-28 — "device online, live view blank".

Reproduces the exact race that left the mirror black while the device kept
reporting online: systemd starts the agent at boot, so ScreenshotCapture is
constructed BEFORE the desktop compositor has created its wayland socket.
Pre-fix, the one-shot probe in __init__ cached None and capture() never looked
at Wayland again for the lifetime of the process — every frame fell through to
scrot, which cannot capture Wayland.

Runs on any Linux box; no Pi, no real compositor, no network needed. `grim` is
faked with a shell script that copies a generated PNG.

Run:  python3 pi-agent/agent/tests/test_wayland_redetect.py
"""
import os
import sys
import shutil
import tempfile

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AGENT_DIR)

WORK = tempfile.mkdtemp(prefix="guardian_wl_")
BIN = os.path.join(WORK, "bin")
RUNTIME = os.path.join(WORK, "runtime")
os.makedirs(BIN)
os.makedirs(RUNTIME)

from PIL import Image  # noqa: E402

SRC_PNG = os.path.join(WORK, "frame.png")
Image.new("RGB", (320, 200), (12, 34, 56)).save(SRC_PNG)

# Fake `grim`: writes the PNG to the path it is given, like the real one.
GRIM = os.path.join(BIN, "grim")
with open(GRIM, "w") as fh:
    fh.write("#!/bin/sh\nexec cp %s \"$1\"\n" % SRC_PNG)
os.chmod(GRIM, 0o755)

os.environ["PATH"] = BIN + os.pathsep + os.environ.get("PATH", "")
os.environ["XDG_RUNTIME_DIR"] = RUNTIME
os.environ["WAYLAND_DISPLAY"] = "wayland-0"
os.environ.pop("DISPLAY", None)  # systemd service: no X display
SOCKET = os.path.join(RUNTIME, "wayland-0")

import screenshot  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(label)


print("test: compositor socket appears after the agent starts")
shot = screenshot.ScreenshotCapture(quality=50, max_width=800)

check("no Wayland target at construction (socket not up yet)",
      shot._wayland_grim_target is None, repr(shot._wayland_grim_target))
check("capture() yields nothing while the session is absent — the reported symptom",
      shot.capture() is None)

# Compositor finishes starting.
open(SOCKET, "w").close()

check("probe is throttled, so it is not re-run on every frame",
      shot.capture() is None and shot._wayland_grim_target is None)

shot._wayland_detect_at -= screenshot.WAYLAND_REDETECT_SEC + 1  # let the throttle expire
frame = shot.capture()
check("capture() recovers once the socket exists (pre-fix: stayed None forever)",
      bool(frame) and frame[:2] == b"\xff\xd8",
      f"{len(frame) if frame else 0} bytes")
check("target adopted and remembered",
      bool(shot._wayland_grim_target)
      and shot._wayland_grim_target.get("display") == "wayland-0")

print("test: session goes away again (logout / compositor restart)")
os.unlink(SOCKET)
os.remove(GRIM)  # make the stale target unusable
shot._wayland_detect_at -= screenshot.WAYLAND_REDETECT_SEC + 1
shot.capture()
check("stale target dropped instead of being retried forever",
      shot._wayland_grim_target is None, repr(shot._wayland_grim_target))

print("test: detection never raises out of capture()")
shot._wayland_detect_at = 0.0
try:
    shot.capture()
    check("capture() stays exception-free with no backend at all", True)
except Exception as exc:  # pragma: no cover
    check("capture() stays exception-free with no backend at all", False, repr(exc))

shot.close()
shutil.rmtree(WORK, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} check(s) failed: {failures}")
    sys.exit(1)
print("all checks passed")
