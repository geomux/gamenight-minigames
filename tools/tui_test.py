#!/usr/bin/env python3
"""
tui_test.py — drives the host terminal dashboard through a real pty,
like a human at a keyboard: renders, navigates with arrow keys, changes
settings, starts a bot round, aborts it, quits cleanly.

    python3 tools/tui_test.py
"""

import fcntl
import os
import pty
import secrets
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILS = []


def check(cond, label):
    print(("  ✓ " if cond else "  ✗ FAIL: ") + label)
    if not cond:
        FAILS.append(label)


def main():
    port = 20000 + secrets.randbelow(20000)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 38, 110, 0, 0))
    env = dict(os.environ, TERM="xterm-256color")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server" / "main.py"),
         "--port", str(port), "--host-password", "TESTPW"],
        stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
    os.close(slave)

    buf = b""

    def read_for(secs):
        nonlocal buf
        got = b""
        end = time.monotonic() + secs
        while time.monotonic() < end:
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                got += chunk
        buf += got
        return got

    def send(data, wait=0.8):
        os.write(master, data)
        return read_for(wait)

    try:
        print("== dashboard boots ==")
        read_for(2.5)
        check(b"GAME NIGHT" in buf, "title rendered")
        check(b"START ROUND" in buf, "menu rendered")
        check(b"TESTPW" in buf, "host password shown")
        check(b"host pw" in buf, "header labels present")
        check(b"Traceback" not in buf, "no crash on boot")

        print("== start with nobody in: clear reason, not a dead end ==")
        out = send(b"s", 1.0)
        check(b"add bots" in out or b"need at least" in out,
              "explains why it can't start")

        print("== arrow-key navigation + setting change ==")
        out = send(b"\x1b[B\x1b[B", 0.6)          # down to Game, down to first setting
        out = send(b"\x1b[C", 0.8)                # right: round cap 90 -> 120
        check(b"round_time = 120" in buf, "arrow keys changed a setting")

        print("== add bots from the menu, start a real round ==")
        # from round_time down 8 rows to Bots
        # (shrink, speed, ice, reverse, bodies, dash, wind, bots)
        send(b"\x1b[B" * 8, 0.6)
        out = send(b"\x1b[C\x1b[C", 1.0)          # bots 0 -> 2
        check(b"bots = 2" in buf, "bots setting via arrows")
        check(b"BOT" in buf, "bot players visible in dashboard")
        out = send(b"s", 4.5)                      # start -> countdown -> playing
        check(b"round 1: Sumo Ring" in buf, "round started from dashboard")
        check(b"ABORT ROUND" in out or b"ABORT ROUND" in buf, "in-round menu shown")
        out = send(b"a", 1.2)                      # abort
        check(b"round aborted" in buf, "abort works")
        check(b"Traceback" not in buf, "no crashes during play")

        print("== quit restores the terminal ==")
        send(b"q", 0.5)
        rc = proc.wait(timeout=8)
        read_for(0.5)
        check(rc == 0, f"clean exit (rc={rc})")
        check(b"?1049l" in buf, "alt screen restored")
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        os.close(master)

    print("=" * 40)
    if FAILS:
        print(f"FAILED — {len(FAILS)} problem(s)")
        sys.exit(1)
    print("TUI TEST PASSED ✔")


if __name__ == "__main__":
    main()
