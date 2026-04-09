"""
Guardian Shield — Cross-platform Buzzer / Alert Sound System
Level 1 = soft warning beep
Level 2 = double alert beep
Level 3 = loud rapid alarm (app killed / final warning)
"""

import io
import math
import os
import platform
import struct
import subprocess
import tempfile
import threading
import time
import wave
import logging

logger = logging.getLogger("guardian.buzzer")


# ── WAV Generation ──────────────────────────────────────────────────────────

def _make_wav(frequency: int, duration_ms: int, volume: float = 0.85) -> bytes:
    """Generate a pure sine-wave tone as raw WAV bytes."""
    rate = 44100
    n = int(rate * duration_ms / 1000)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            # Sine wave with short fade-in/out to avoid clicks
            fade = min(1.0, i / (rate * 0.01), (n - i) / (rate * 0.01))
            val = int(volume * fade * 32767 * math.sin(2 * math.pi * frequency * i / rate))
            frames += struct.pack('<h', max(-32768, min(32767, val)))
        w.writeframes(bytes(frames))
    return buf.getvalue()


def _play_wav_linux(wav_bytes: bytes):
    """Write WAV to temp file and play using available Linux audio backend."""
    fd, path = tempfile.mkstemp(suffix='.wav')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(wav_bytes)
        for cmd in [
            ['aplay', '-q', path],
            ['paplay', path],
            ['sox', path, '-d'],
            ['ffplay', '-nodisp', '-autoexit', path],
        ]:
            try:
                r = subprocess.run(cmd, timeout=4, capture_output=True)
                if r.returncode == 0:
                    return
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
    except Exception as e:
        logger.debug(f"Linux audio error: {e}")
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


# ── Platform-specific playback ───────────────────────────────────────────────

def _play_windows(level: int):
    import winsound
    if level == 1:
        # Single soft beep
        winsound.Beep(880, 350)
    elif level == 2:
        # Double alert
        winsound.Beep(1100, 300)
        time.sleep(0.08)
        winsound.Beep(700, 300)
    else:
        # Rapid alarm — 6 alternating high-low beeps
        for _ in range(6):
            winsound.Beep(1600, 140)
            winsound.Beep(900, 140)


def _play_linux(level: int):
    env = os.environ.copy()
    env.setdefault('DISPLAY', ':99')

    if level == 1:
        _play_wav_linux(_make_wav(880, 380))
    elif level == 2:
        _play_wav_linux(_make_wav(1100, 300))
        time.sleep(0.09)
        _play_wav_linux(_make_wav(700, 300))
    else:
        for _ in range(6):
            _play_wav_linux(_make_wav(1600, 140))
            time.sleep(0.06)
            _play_wav_linux(_make_wav(900, 140))
            time.sleep(0.06)


# ── Public API ───────────────────────────────────────────────────────────────

def play_warning(level: int = 1):
    """
    Play a buzzer/alarm sound in a background thread (non-blocking).

    Args:
        level: 1 = soft warning, 2 = moderate alert, 3 = loud alarm
    """
    def _run():
        try:
            if platform.system() == "Windows":
                _play_windows(level)
            else:
                _play_linux(level)
        except Exception as e:
            logger.error(f"play_warning failed: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    # Quick test — play all three levels
    logging.basicConfig(level=logging.DEBUG)
    for lvl in [1, 2, 3]:
        print(f"Playing level {lvl}...")
        t = play_warning(lvl)
        t.join()
        time.sleep(0.5)
    print("Done.")
