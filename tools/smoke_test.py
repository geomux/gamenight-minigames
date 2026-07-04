#!/usr/bin/env python3
"""
smoke_test.py — headless end-to-end test of the whole game.

Boots the real server, connects real websocket clients (masked frames, like a
browser), plays a full bot round, checks scoring, reconnect, terminal
commands, kick, and the join-password gate. No browser needed.

    python3 tools/smoke_test.py
"""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

FAILS = []


def check(cond, label):
    print(("  ✓ " if cond else "  ✗ FAIL: ") + label)
    if not cond:
        FAILS.append(label)
    return cond


class WSClient:
    """Just enough RFC 6455 to act like a browser client."""

    def __init__(self, name):
        self.name = name
        self.q = asyncio.Queue()
        self.closed = False
        self.snap_count = 0
        self.cells_seen = 0
        self.fx = []
        self.last_snap = None

    async def connect(self, port):
        self.r, self.w = await asyncio.open_connection("127.0.0.1", port)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.w.write(
            (f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
             "Upgrade: websocket\r\nConnection: Upgrade\r\n"
             f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = await self.r.readuntil(b"\r\n\r\n")
        assert b"101" in head.split(b"\r\n")[0], f"handshake failed: {head[:80]}"
        want = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        assert want.encode() in head, "bad Sec-WebSocket-Accept"
        self.task = asyncio.create_task(self._reader())

    def _frame(self, opcode, payload: bytes):
        mask = os.urandom(4)
        head = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += n.to_bytes(2, "big")
        else:
            head.append(0x80 | 127)
            head += n.to_bytes(8, "big")
        head += mask
        return bytes(head) + bytes(b ^ mask[i & 3] for i, b in enumerate(payload))

    def send(self, obj):
        if not self.closed:
            self.w.write(self._frame(0x1, json.dumps(obj).encode()))

    async def _reader(self):
        try:
            while True:
                h = await self.r.readexactly(2)
                op = h[0] & 0x0F
                n = h[1] & 0x7F
                if n == 126:
                    n = int.from_bytes(await self.r.readexactly(2), "big")
                elif n == 127:
                    n = int.from_bytes(await self.r.readexactly(8), "big")
                payload = await self.r.readexactly(n) if n else b""
                if op == 0x9:                       # ping -> pong
                    self.w.write(self._frame(0xA, payload))
                elif op == 0x8:
                    break
                elif op == 0x1:
                    m = json.loads(payload.decode())
                    if m.get("t") == "s":
                        self.snap_count += 1
                        self.last_snap = m
                        self.cells_seen += len(m.get("cells") or [])
                    elif m.get("t") == "fx":
                        self.fx.extend(m["ev"])
                    else:
                        self.q.put_nowait(m)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self.closed = True

    async def expect(self, mtype, timeout=10, where=None):
        """Wait for the next message of type `mtype` (optionally matching a
        predicate), letting others pass by."""
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{self.name}: no '{mtype}' within {timeout}s")
            m = await asyncio.wait_for(self.q.get(), left)
            if m.get("t") == mtype and (where is None or where(m)):
                return m

    async def drain(self):
        while not self.q.empty():
            self.q.get_nowait()

    def kill(self):
        self.closed = True
        try:
            self.w.close()
        except Exception:
            pass


def http_get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


async def main_flow(port, proc):
    print("\n== static http ==")
    code, body = http_get(port, "/")
    check(code == 200 and b"GAME NIGHT" in body, "GET / serves the client")
    code, _ = http_get(port, "/js/render.js")
    check(code == 200, "GET /js/render.js 200")

    print("\n== lobby & host ==")
    host = WSClient("host")
    await host.connect(port)
    hello = await host.expect("hello")
    check(hello["needPw"] is False, "hello: no join password needed")
    host.send({"t": "join", "name": "Hosty", "pw": "TESTPW", "sess": None})
    joined = await host.expect("joined")
    check(joined["you"]["host"] is True, "host password grants host")
    host_sess = joined["sess"]

    bob = WSClient("bob")
    await bob.connect(port)
    await bob.expect("hello")
    bob.send({"t": "join", "name": "Bob", "pw": "", "sess": None})
    bj = await bob.expect("joined")
    bob_pid = bj["you"]["id"]
    check(bj["you"]["host"] is False, "regular player is not host")
    room = await host.expect("room", where=lambda m: len(m["players"]) == 2)
    check(room["state"] == "lobby", "room state is lobby")

    print("\n== settings ==")
    host.send({"t": "host", "a": "set", "k": "round_time", "v": 30})
    host.send({"t": "host", "a": "set", "k": "shrink", "v": "fast"})
    host.send({"t": "host", "a": "set", "k": "bot_skill", "v": "mean"})
    host.send({"t": "host", "a": "set", "k": "bots", "v": 2})
    room = await bob.expect("room", where=lambda m: m["settings"].get("bots") == 2, timeout=5)
    check(room["settings"]["round_time"] == 30, "round_time applied")
    check(room["settings"]["shrink"] == "fast", "shrink applied")
    check(sum(1 for p in room["players"] if p["bot"]) == 2, "2 bots joined the lobby")
    # non-host can't change settings
    bob.send({"t": "host", "a": "set", "k": "bots", "v": 6})
    await asyncio.sleep(0.5)
    await bob.drain()

    print("\n== round 1 (started via websocket host panel) ==")
    host.send({"t": "host", "a": "start"})
    rnd = await bob.expect("round", timeout=5)
    check(rnd["phase"] == "countdown", "round starts with countdown")
    check(rnd["game"]["id"] == "sumo", "game is sumo")
    check(len(rnd["roster"]) == 4, "roster = 2 humans + 2 bots")
    check(rnd["arena"]["R0"] > 0, "arena payload present")
    await bob.expect("go", timeout=6)
    print("  ✓ go received")

    # bob mashes movement keys while the bots brawl
    async def mash():
        for i in range(120):
            if bob.closed:
                return
            bob.send({"t": "input",
                      "k": {"u": i % 7 < 3, "d": False, "l": i % 11 < 4,
                            "r": i % 5 < 2, "a": i % 20 == 0}})
            await asyncio.sleep(0.1)
    mash_task = asyncio.create_task(mash())

    end = await bob.expect("end", timeout=45)
    mash_task.cancel()
    check(len(end["placements"]) == 4, "4 placements")
    check(min(p[1] for p in end["placements"]) == 1, "someone got 1st")
    check(len(end["winner"]) >= 1, "winner announced")
    pts = {pid: pts for pid, place, pts in end["placements"]}
    check(all(v >= 1 for v in pts.values()), "everyone scored points")
    check(bob.snap_count > 50, f"snapshots streamed ({bob.snap_count})")
    kinds = {e[0] for e in bob.fx}
    check("fall" in kinds, f"fx events flowed ({sorted(kinds)})")
    bob_pts = pts.get(bob_pid, 0)

    room = await bob.expect("room", where=lambda m: m["state"] == "lobby", timeout=20)
    print("  ✓ auto-returned to lobby")
    brow = next(p for p in room["players"] if p["id"] == bob_pid)
    check(brow["pts"] == bob_pts, "scoreboard totals match round points")

    print("\n== reconnect keeps seat & score ==")
    bob_sess = bj["sess"]
    bob.kill()  # hard drop, like wifi dying
    await asyncio.sleep(0.6)
    bob2 = WSClient("bob2")
    await bob2.connect(port)
    await bob2.expect("hello")
    bob2.send({"t": "join", "name": "Bob", "pw": "", "sess": bob_sess})
    j2 = await bob2.expect("joined")
    check(j2["you"]["id"] == bob_pid, "same player id after reconnect")
    room = await bob2.expect("room")
    brow = next(p for p in room["players"] if p["id"] == bob_pid)
    check(brow["pts"] == bob_pts, "points survived the reconnect")

    print("\n== round 2 (started from the server terminal) ==")
    proc.stdin.write(b"start\n")
    await proc.stdin.drain()
    rnd = await bob2.expect("round", timeout=5)
    check(rnd["round"] == 2, "terminal 'start' works")
    proc.stdin.write(b"abort\n")
    await proc.stdin.drain()
    room = await bob2.expect("room", where=lambda m: m["state"] == "lobby", timeout=5)
    print("  ✓ terminal 'abort' returns to lobby")

    print("\n== kick ==")
    host.send({"t": "host", "a": "kick", "p": bob_pid})
    kicked = await bob2.expect("kicked", timeout=5)
    check("kicked" in kicked["msg"].lower(), "kicked player notified")

    print("\n== round 3: light cycles (game #2 via the lobby menu) ==")
    host.send({"t": "host", "a": "set_game", "g": "cycles"})
    room = await host.expect("room", where=lambda m: m["gameId"] == "cycles", timeout=5)
    check(any(g["id"] == "cycles" for g in room["games"]), "cycles listed in game menu")
    host.send({"t": "host", "a": "set", "k": "bots", "v": 3})
    host.send({"t": "host", "a": "set", "k": "round_time", "v": 30})
    host.send({"t": "host", "a": "set", "k": "shrink", "v": "fast"})
    await host.expect("room", where=lambda m: m["settings"].get("bots") == 3, timeout=5)
    host.snap_count = 0
    host.cells_seen = 0
    host.fx = []
    host.send({"t": "host", "a": "start"})
    rnd = await host.expect("round", timeout=5)
    check(rnd["game"]["id"] == "cycles", "cycles round started")
    check(rnd["arena"]["gw"] == 96, "grid arena payload")
    check(bool(rnd.get("preview", {}).get("heads")), "spawn preview in round msg")
    await host.expect("go", timeout=6)
    end = await host.expect("end", timeout=45)
    check(len(end["placements"]) == 4, "cycles: 4 placements")
    check(min(p[1] for p in end["placements"]) == 1, "cycles: someone won")
    check(host.cells_seen > 100, f"trail cell deltas streamed ({host.cells_seen})")
    check(any(e[0] == "die" for e in host.fx), "cycles die events")

    print("\n== round 4: avalanche run ==")
    host.send({"t": "host", "a": "set_game", "g": "ski"})
    await host.expect("room", where=lambda m: m["gameId"] == "ski", timeout=5)
    host.send({"t": "host", "a": "set", "k": "round_time", "v": 60})
    host.send({"t": "host", "a": "set", "k": "ramp", "v": "wild"})
    host.send({"t": "host", "a": "set", "k": "bots", "v": 3})   # settings are per-game
    host.send({"t": "host", "a": "set", "k": "bot_skill", "v": "mean"})
    await host.expect("room", where=lambda m: m["settings"].get("bots") == 3, timeout=5)
    host.fx = []
    host.send({"t": "host", "a": "start"})
    rnd = await host.expect("round", timeout=5)
    check(rnd["game"]["id"] == "ski", "ski round started")
    check(rnd["arena"].get("action") == "SNOWBALL", "ski arena drives the charge meter")
    check(bool(rnd.get("preview", {}).get("obs")), "terrain preview in round msg")
    await host.expect("go", timeout=6)
    end = await host.expect("end", timeout=75)
    check(len(end["placements"]) == 4, "ski: 4 placements")
    kinds = {e[0] for e in host.fx}
    check("wipe" in kinds, f"ski fx flowed ({sorted(kinds)})")
    snap = host.last_snap or {}
    check(snap.get("g") == "ski" and "cam" in snap, "ski snapshots carry the camera")

    print("\n== round 5: aces high ==")
    host.send({"t": "host", "a": "set_game", "g": "planes"})
    await host.expect("room", where=lambda m: m["gameId"] == "planes", timeout=5)
    host.send({"t": "host", "a": "set", "k": "round_time", "v": 60})
    host.send({"t": "host", "a": "set", "k": "guns", "v": "blaster"})
    host.send({"t": "host", "a": "set", "k": "bots", "v": 3})   # settings are per-game
    host.send({"t": "host", "a": "set", "k": "bot_skill", "v": "mean"})
    await host.expect("room", where=lambda m: m["settings"].get("bots") == 3, timeout=5)
    host.fx = []
    host.send({"t": "host", "a": "start"})
    rnd = await host.expect("round", timeout=5)
    check(rnd["game"]["id"] == "planes", "planes round started")
    check(rnd["arena"].get("action") == "FIRE", "planes arena drives the charge meter")
    check(len(rnd["arena"].get("islands", [])) == 3, "floating islands in arena")
    check(len(rnd["arena"].get("gusts", [])) == 2, "wind gusts in arena")
    await host.expect("go", timeout=6)

    # host pause menu: freeze the whole sim, then resume
    host.send({"t": "host", "a": "pause"})
    pmsg = await host.expect("pause", timeout=5)
    check(pmsg["on"] is True, "pause broadcast")
    await asyncio.sleep(0.4)
    n0 = host.snap_count
    await asyncio.sleep(1.0)
    check(host.snap_count == n0, "snapshots freeze while paused")
    host.send({"t": "host", "a": "pause", "v": False})
    pmsg = await host.expect("pause", timeout=5)
    check(pmsg["on"] is False, "resume broadcast")
    await asyncio.sleep(1.0)
    check(host.snap_count > n0, "snapshots flow again after resume")

    end = await host.expect("end", timeout=75)
    check(len(end["placements"]) == 4, "planes: 4 placements")
    kinds = {e[0] for e in host.fx}
    check("shoot" in kinds and ("hitp" in kinds or "down" in kinds),
          f"planes fx flowed ({sorted(kinds)})")

    print("\n== pause menu 'end round' (ws host action) ==")
    host.send({"t": "host", "a": "set_game", "g": "sumo"})
    await host.expect("room", where=lambda m: m["gameId"] == "sumo", timeout=5)
    host.send({"t": "host", "a": "start"})
    await host.expect("round", timeout=5)
    await host.expect("go", timeout=6)
    host.send({"t": "host", "a": "pause"})
    await host.expect("pause", timeout=5)
    host.send({"t": "host", "a": "abort"})
    await host.expect("room", where=lambda m: m["state"] == "lobby", timeout=5)
    print("  ✓ pause → END ROUND returns everyone to the lobby")

    host.kill()
    return True


async def pw_flow():
    """Second server with a join password: gate works both ways."""
    print("\n== join-password gate (second server) ==")
    port = 20000 + secrets.randbelow(20000)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", str(ROOT / "server" / "main.py"),
        "--port", str(port), "--host-password", "TESTPW",
        "--join-password", "sesame",
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        await asyncio.sleep(1.0)
        c = WSClient("gate")
        await c.connect(port)
        hello = await c.expect("hello")
        check(hello["needPw"] is True, "hello says password required")
        c.send({"t": "join", "name": "Randy", "pw": "wrong", "sess": None})
        err = await c.expect("err", timeout=5)
        check("password" in err["msg"].lower(), "wrong password rejected")
        c.send({"t": "join", "name": "Randy", "pw": "sesame", "sess": None})
        await c.expect("joined", timeout=5)
        print("  ✓ right password admitted")
        c.kill()
    finally:
        proc.terminate()
        await proc.wait()


async def main():
    port = 20000 + secrets.randbelow(20000)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", str(ROOT / "server" / "main.py"),
        "--port", str(port), "--host-password", "TESTPW",
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    async def tail():
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            sys.stdout.write("    [server] " + line.decode(errors="replace"))

    tail_task = asyncio.create_task(tail())
    try:
        await asyncio.sleep(1.0)
        await main_flow(port, proc)
        await pw_flow()
    finally:
        tail_task.cancel()
        proc.terminate()
        await proc.wait()

    print("\n" + ("=" * 46))
    if FAILS:
        print(f"FAILED — {len(FAILS)} problem(s):")
        for f in FAILS:
            print("  ✗ " + f)
        sys.exit(1)
    print("ALL CHECKS PASSED ✔")


if __name__ == "__main__":
    asyncio.run(main())
