"""
tui.py — interactive host dashboard for the server terminal. Zero deps.

Arrow keys navigate the menu, ←/→ (or Enter) change values, Enter fires
actions. Live players/scoreboard/log, all in color. Used automatically when
the server runs in a real terminal; piped/nohup'd servers fall back to the
plain line console (so scripts and tests keep working).

Raw ANSI on the alternate screen buffer — no curses, nothing to install.
"""

import asyncio
import os
import re
import shutil
import signal
import sys
import termios
import time
import tty

from minigames import GAMES

CSI = "\x1b["
ALT_ON = CSI + "?1049h" + CSI + "?25l"    # alt screen, hide cursor
ALT_OFF = CSI + "?1049l" + CSI + "?25h"

RESET = CSI + "0m"
BOLD = CSI + "1m"
DIM = CSI + "2m"


def fg(n):
    return f"{CSI}38;5;{n}m"


GOLD, CYAN, GREEN, RED, GREY, PURP, WHITE = (
    fg(220), fg(51), fg(84), fg(203), fg(245), fg(141), fg(255))

STATE_STYLE = {"lobby": CYAN, "countdown": GOLD, "playing": GREEN, "results": PURP}

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def vlen(s):
    return len(_ANSI_RE.sub("", s))


def pad(s, w):
    n = vlen(s)
    if n >= w:
        return s if n == w else _ANSI_RE.sub("", s)[:w]
    return s + " " * (w - n)


def hex256(hexcolor):
    """Map a #rrggbb player color onto the xterm 256 cube."""
    try:
        n = int(str(hexcolor).lstrip("#"), 16)
        r, g, b = (n >> 16) & 255, (n >> 8) & 255, n & 255
        return fg(16 + 36 * round(r / 51) + 6 * round(g / 51) + round(b / 51))
    except (ValueError, TypeError):
        return WHITE


class Tui:
    def __init__(self, room, cfg, ip):
        self.room = room
        self.cfg = cfg
        self.ip = ip
        self.loop = asyncio.get_event_loop()
        self.fd = sys.stdin.fileno()
        self.logs = []
        self.dirty = asyncio.Event()
        self.sel = 0
        self.kick_sel = 0
        self.mode = "menu"          # menu | kick | text
        self.text = ""
        self.status_msg = ""
        self.buf = b""

    # ---------------------------------------------------------------- logging

    def log(self, msg):
        msg = str(msg)
        if msg.startswith("[gn] "):
            msg = msg[5:]
        self.logs.append((time.strftime("%H:%M:%S"), msg))
        self.logs = self.logs[-12:]
        self.dirty.set()

    # ------------------------------------------------------------------ menu

    def menu_items(self):
        st = self.room.state
        say = {"k": "say", "kind": "action", "label": "Say something…"}
        quit_ = {"k": "quit", "kind": "action", "label": "Quit server", "style": RED}
        if st == "lobby":
            items = [{"k": "start", "kind": "action", "label": "START ROUND",
                      "style": GREEN + BOLD},
                     {"kind": "game"},
                     {"kind": "div", "label": "game settings"}]
            for s in GAMES[self.room.game_id].settings_schema():
                items.append({"kind": "set", "s": s})
            items += [{"kind": "div", "label": "actions"},
                      {"k": "kick", "kind": "action", "label": "Kick player…"},
                      say,
                      {"k": "reset", "kind": "action", "label": "Reset scores"},
                      quit_]
            return items
        if st in ("countdown", "playing"):
            return [{"k": "abort", "kind": "action", "label": "ABORT ROUND",
                     "style": RED + BOLD}, say, quit_]
        return [{"k": "start", "kind": "action", "label": "REMATCH (same settings)",
                 "style": GREEN + BOLD},
                {"k": "lobby", "kind": "action", "label": "Back to lobby"},
                say,
                {"k": "reset", "kind": "action", "label": "Reset scores"},
                quit_]

    @staticmethod
    def _selectable(item):
        return item.get("kind") in ("action", "game", "set")

    def _clamp_sel(self, items):
        if not items:
            self.sel = 0
            return
        self.sel %= len(items)
        if not self._selectable(items[self.sel]):
            self._move(items, 1)

    def _move(self, items, d):
        for _ in range(len(items)):
            self.sel = (self.sel + d) % len(items)
            if self._selectable(items[self.sel]):
                return

    # ----------------------------------------------------------------- input

    def _on_stdin(self):
        try:
            data = os.read(self.fd, 128)
        except OSError:
            return
        if not data:
            return
        self.buf += data
        while self.buf:
            key, self.buf = self._pop_key(self.buf)
            if key is None:
                break
            try:
                self._key(key)
            except SystemExit:
                self._quit()
                return
        self.dirty.set()

    @staticmethod
    def _pop_key(buf):
        if buf.startswith(b"\x1b"):
            if buf == b"\x1b":
                return "esc", b""
            if len(buf) >= 3 and buf[1:2] == b"[":
                code = {b"A": "up", b"B": "down", b"C": "right", b"D": "left"}.get(buf[2:3])
                return (code or "?"), buf[3:]
            if len(buf) < 3:
                return None, buf          # wait for the rest of the sequence
            return "?", buf[2:]
        ch = buf[:1].decode("utf-8", "ignore")
        return (ch or "?"), buf[1:]

    def _key(self, k):
        if self.mode == "text":
            if k == "esc":
                self.mode = "menu"
            elif k in ("\r", "\n"):
                msg = self.text.strip()
                if msg:
                    self.room._toast("HOST: " + msg)
                    self.log(f"say: {msg}")
                self.text = ""
                self.mode = "menu"
            elif k == "\x7f":
                self.text = self.text[:-1]
            elif len(k) == 1 and k.isprintable():
                self.text += k
            return

        if self.mode == "kick":
            targets = list(self.room.players.values())
            if not targets or k == "esc":
                self.mode = "menu"
                return
            self.kick_sel %= len(targets)
            if k == "up":
                self.kick_sel = (self.kick_sel - 1) % len(targets)
            elif k == "down":
                self.kick_sel = (self.kick_sel + 1) % len(targets)
            elif k in ("\r", "\n"):
                self.room.kick(targets[self.kick_sel])
                self.mode = "menu"
            return

        # menu mode
        items = self.menu_items()
        self._clamp_sel(items)
        if k == "up":
            self._move(items, -1)
        elif k == "down":
            self._move(items, 1)
        elif k in ("left", "right", "\r", "\n"):
            d = -1 if k == "left" else 1
            it = items[self.sel]
            if it["kind"] == "set":
                self._cycle_setting(it["s"], d)
            elif it["kind"] == "game":
                self._cycle_game(d)
            elif it["kind"] == "action" and k in ("\r", "\n"):
                self._action(it["k"])
        elif k == "s":
            self._action("start")
        elif k == "a" and self.room.state in ("countdown", "playing"):
            self._action("abort")
        elif k == "q":
            self._action("quit")

    def _cycle_setting(self, s, d):
        if self.room.state not in ("lobby", "results"):
            self.status_msg = "settings are locked while a round is live"
            return
        cur = self.room.cur_settings()[s["k"]]
        if s["type"] == "bool":
            v = not cur
        elif s["type"] == "int":
            v = max(s["min"], min(s["max"], cur + d))
        else:
            ch = s["choices"]
            v = ch[(ch.index(cur) + d) % len(ch)] if cur in ch else ch[0]
        self.status_msg = ""
        self.room.set_setting(s["k"], v)

    def _cycle_game(self, d):
        ids = list(GAMES)
        cur = ids.index(self.room.game_id)
        self.status_msg = ""
        self.room.set_game(ids[(cur + d) % len(ids)])

    def _action(self, k):
        self.status_msg = ""
        if k == "start":
            err = self.room.start_round(by="terminal")
            self.status_msg = err or ""
        elif k == "abort":
            self.room.abort_round()
        elif k == "lobby":
            self.room.to_lobby()
        elif k == "reset":
            self.room.reset_scores()
        elif k == "kick":
            if self.room.players:
                self.mode = "kick"
                self.kick_sel = 0
            else:
                self.status_msg = "nobody to kick"
        elif k == "say":
            self.mode = "text"
            self.text = ""
        elif k == "quit":
            self._quit()

    def _quit(self):
        for t in asyncio.all_tasks(self.loop):
            t.cancel()

    # ------------------------------------------------------------------ draw

    def _draw(self):
        room = self.room
        w = min(shutil.get_terminal_size((100, 32)).columns, 110)
        if w < 44:
            sys.stdout.write(CSI + "H" + CSI + "2J terminal too small\r\n")
            sys.stdout.flush()
            return
        st = room.state
        sc = STATE_STYLE.get(st, WHITE)
        L = []

        # header
        left = f" {GOLD}{BOLD}▚▚ GAME NIGHT{RESET}  {sc}{BOLD}{st.upper()}{RESET}"
        right = f"{GREY}round {room.round_no} · {GAMES[room.game_id].NAME}{RESET} "
        L.append(pad(left, w - vlen(right)) + right)
        L.append(f" {GREY}{'─' * (w - 2)}{RESET}")
        jp = self.cfg.get("join_password") or ""
        L.append(f"  {DIM}join{RESET} {CYAN}http://{self.ip}:{self.cfg['port']}{RESET}"
                 f"   {DIM}host pw{RESET} {GOLD}{self.cfg['host_password']}{RESET}"
                 + (f"   {DIM}join pw{RESET} {GOLD}{jp}{RESET}" if jp else ""))

        # live round line
        now = self.loop.time()
        if st == "countdown":
            L.append(f"  {GOLD}starting in {max(0, int(room._phase_end - now) + 1)}…{RESET}")
        elif st == "playing" and room.game:
            L.append(f"  {GREEN}⚔ {room.game.status()}{RESET}")
        elif st == "results":
            L.append(f"  {PURP}results — lobby in {max(0, int(room._phase_end - now))}s{RESET}")
        else:
            humans = sum(1 for p in room.players.values() if not p.is_bot and p.conn)
            L.append(f"  {DIM}waiting in the lobby · {humans} connected{RESET}")
        L.append("")

        # players / scoreboard columns
        lw = max(26, w // 2 - 2)
        plist = list(room.players.values())
        lrows = [f" {BOLD}PLAYERS{RESET} {GREY}({sum(1 for p in plist if not p.is_bot)}/"
                 f"{self.cfg['max_players']}){RESET}"]
        for p in plist[:9]:
            dot = hex256(p.color) + "●" + RESET
            tag = (f" {GOLD}HOST{RESET}" if p.is_host else "") + \
                  (f" {PURP}BOT{RESET}" if p.is_bot else "") + \
                  ("" if p.connected else f" {RED}offline{RESET}")
            lrows.append(f"  {dot} {p.name}{tag}")
        if len(plist) > 9:
            lrows.append(f"  {DIM}+{len(plist) - 9} more{RESET}")
        if not plist:
            lrows.append(f"  {DIM}nobody yet — share the join URL{RESET}")

        rrows = [f" {BOLD}SCOREBOARD{RESET}"]
        ranked = sorted(plist, key=lambda p: (-p.wins, -p.points))
        for i, p in enumerate(ranked[:9]):
            crown = "👑" if i == 0 and p.wins > 0 else "  "
            rrows.append(f" {crown} {hex256(p.color)}{pad(p.name, 15)}{RESET}"
                         f"{p.wins}W {GREY}{p.points}p{RESET}")
        if not ranked:
            rrows.append(f"  {DIM}—{RESET}")

        for i in range(max(len(lrows), len(rrows))):
            a = lrows[i] if i < len(lrows) else ""
            b = rrows[i] if i < len(rrows) else ""
            L.append(pad(a, lw) + b)
        L.append("")

        # menu
        hints = {
            "menu": "↑↓ move · ←→ change · enter select · s start · q quit",
            "kick": "↑↓ pick · enter kick · esc back",
            "text": "enter send · esc cancel",
        }[self.mode]
        L.append(f" {BOLD}MENU{RESET}  {DIM}{hints}{RESET}")

        if self.mode == "text":
            L.append(f"   {CYAN}say:{RESET} {self.text}{BOLD}_{RESET}")
        elif self.mode == "kick":
            targets = list(room.players.values())
            self.kick_sel %= max(1, len(targets))
            for i, p in enumerate(targets):
                cur = i == self.kick_sel
                mark = f"{RED}▸{RESET}" if cur else " "
                style = BOLD if cur else DIM
                L.append(f"  {mark} {style}kick {p.name}{RESET}")
        else:
            items = self.menu_items()
            self._clamp_sel(items)
            for i, it in enumerate(items):
                cur = i == self.sel
                mark = f"{GOLD}▸{RESET}" if cur else " "
                if it["kind"] == "div":
                    L.append(f"    {GREY}── {it['label']} ──{RESET}")
                elif it["kind"] == "game":
                    val = GAMES[room.game_id].NAME
                    style = BOLD + CYAN if cur else CYAN
                    L.append(f"  {mark} {pad('Game', 17)}{style}‹ {val} ›{RESET}")
                elif it["kind"] == "set":
                    s = it["s"]
                    v = room.cur_settings()[s["k"]]
                    vs = {True: "on", False: "off"}.get(v, str(v))
                    vcol = GOLD if v != s["def"] else WHITE
                    style = BOLD if cur else ""
                    L.append(f"  {mark} {style}{pad(s['label'], 17)}{RESET}"
                             f"{style}{vcol}‹ {vs} ›{RESET}")
                else:
                    style = it.get("style", "") + (BOLD if cur else "")
                    L.append(f"  {mark} {style}{it['label']}{RESET}")

        if self.status_msg:
            L.append(f"   {RED}⚠ {self.status_msg}{RESET}")
        L.append("")

        # log
        L.append(f" {BOLD}LOG{RESET}")
        for ts, msg in self.logs[-6:] or [("", "…")]:
            L.append(f"  {DIM}{ts}{RESET} {msg}")

        out = [CSI + "H"]
        for line in L:
            out.append(CSI + "2K" + pad(line, w) + RESET + "\r\n")
        out.append(CSI + "J")
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    # ------------------------------------------------------------------- run

    async def run(self):
        old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        sys.stdout.write(ALT_ON)
        sys.stdout.flush()
        self.loop.add_reader(self.fd, self._on_stdin)
        try:
            self.loop.add_signal_handler(signal.SIGWINCH, self.dirty.set)
        except (NotImplementedError, ValueError, OSError):
            pass

        async def ticker():
            while True:
                await asyncio.sleep(1.0)
                self.dirty.set()

        tick_task = asyncio.create_task(ticker())
        self.log("dashboard ready — type s (or select START ROUND) when everyone's in")
        self.dirty.set()
        try:
            while True:
                await self.dirty.wait()
                self.dirty.clear()
                try:
                    self._draw()
                except Exception:
                    # never leave the host stuck in a broken alt screen
                    import traceback
                    sys.stdout.write(ALT_OFF)
                    sys.stdout.flush()
                    traceback.print_exc()
                    raise
                await asyncio.sleep(0.05)   # coalesce redraw bursts
        finally:
            tick_task.cancel()
            try:
                self.loop.remove_reader(self.fd)
            except (ValueError, OSError):
                pass
            sys.stdout.write(ALT_OFF)
            sys.stdout.flush()
            termios.tcsetattr(self.fd, termios.TCSADRAIN, old)
            print("server stopped — thanks for hosting game night!")
