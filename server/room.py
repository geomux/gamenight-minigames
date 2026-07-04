"""
room.py — the shell: players, lobby, settings, round lifecycle, tick loop.

State machine:  lobby -> countdown(3s) -> playing -> results(14s) -> lobby
Rounds are short and standalone (no elimination-across-rounds; nobody sits
out). A session scoreboard (wins + points) accumulates in the lobby until the
host resets it. The host is whoever typed the host password at the join
screen — plus the server terminal always has full control.
"""

import asyncio
import json
import random
import secrets
import traceback

from minigames import GAMES, GAME_LIST, default_settings

COUNTDOWN = 3.0
RESULTS_SECS = 14.0
KEEPALIVE = 15.0        # ping cadence
DEAD_AFTER = 60.0       # close conns with no traffic for this long
PURGE_AFTER = 600.0     # forget disconnected players after this long

COLORS = ["#ff5a5a", "#ffa53b", "#ffe14d", "#8dff4d", "#3bffb0", "#3bd6ff",
          "#5a8bff", "#a06bff", "#ff6bd6", "#c9ff3b", "#ff8f6b", "#4dfff0"]

BOT_NAMES = ["Paarthurnax", "Dovahkiin", "Ender Dragon", "Herobrine",
             "Leeroy Jenkins", "Saitama", "Naruto", "Kakashi", "Itachi",
             "Gojo", "Luffy", "Zoro", "Goku", "Vegeta", "Kirby", "Waluigi",
             "Bowser", "Ganondorf", "Sans", "GLaDOS", "Master Chief",
             "Doomguy", "Kratos", "Geralt", "Solaire", "Patches",
             "Tom Nook", "Big Smoke", "CJ", "Jigglypuff"]

NO_KEYS = {"u": False, "d": False, "l": False, "r": False, "a": False}


class Player:
    def __init__(self, pid, name, color, is_bot=False):
        self.pid = pid
        self.name = name
        self.color = color
        self.is_bot = is_bot
        self.is_host = False
        self.conn = None
        self.session = secrets.token_urlsafe(9)
        self.wins = 0
        self.points = 0
        self.keys = NO_KEYS.copy()
        self.in_round = False
        self.gone_since = None      # loop-time when they disconnected
        self.last_input = 0.0

    @property
    def connected(self):
        return self.is_bot or self.conn is not None

    def row(self):
        return {"id": self.pid, "name": self.name, "color": self.color,
                "host": self.is_host, "bot": self.is_bot,
                "conn": self.connected, "wins": self.wins, "pts": self.points,
                "inRound": self.in_round}


class Room:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self.loop = asyncio.get_event_loop()
        self.state = "lobby"
        self.players = {}                     # pid -> Player
        self._next_pid = 1
        self.game_id = next(iter(GAMES))
        self.settings = {self.game_id: default_settings(self.game_id)}
        self.game = None
        self.round_no = 0
        self.paused = False
        self._phase_end = 0.0
        self._tick_no = 0
        self._last_ka = 0.0
        self._snap_every = max(1, round(cfg["tick_rate"] / cfg["snapshot_rate"]))

    # ================================================================ helpers

    def _humans(self, connected_only=True):
        return [p for p in self.players.values()
                if not p.is_bot and (p.conn is not None or not connected_only)]

    def _send(self, p, obj):
        if p.conn:
            p.conn.send_text(json.dumps(obj, separators=(",", ":")))

    def _bcast(self, obj):
        text = json.dumps(obj, separators=(",", ":"))
        for p in self.players.values():
            if p.conn:
                p.conn.send_text(text)

    def _bcast_state(self, obj):
        text = json.dumps(obj, separators=(",", ":"))
        for p in self.players.values():
            if p.conn:
                p.conn.send_state(text)

    def _toast(self, msg):
        self._bcast({"t": "toast", "msg": msg})

    def cur_settings(self):
        if self.game_id not in self.settings:
            self.settings[self.game_id] = default_settings(self.game_id)
        return self.settings[self.game_id]

    def room_msg(self):
        cls = GAMES[self.game_id]
        return {"t": "room", "state": self.state, "round": self.round_no,
                "gameId": self.game_id, "games": GAME_LIST,
                "schema": cls.settings_schema(), "settings": self.cur_settings(),
                "players": [p.row() for p in self.players.values()],
                "maxP": self.cfg["max_players"]}

    def bcast_room(self):
        self._bcast(self.room_msg())

    # ================================================================== joins

    def on_ws_open(self, conn):
        conn.send_text(json.dumps(
            {"t": "hello", "needPw": bool(self.cfg["join_password"]),
             "title": "GAME NIGHT"}, separators=(",", ":")))

    def on_ws_close(self, conn):
        p = conn.player
        conn.player = None
        if not p or p.conn is not conn:
            return
        p.conn = None
        p.keys = NO_KEYS.copy()
        p.gone_since = self.loop.time()
        # fresh lobby players who bail just disappear; anyone with history is
        # kept so a reload can reclaim their seat and score
        if self.state == "lobby" and p.wins == 0 and p.points == 0 and not p.in_round:
            self.players.pop(p.pid, None)
        self.log(f"[gn] left: {p.name}")
        self.bcast_room()

    def _sanitize_name(self, raw, keep_pid=None):
        name = "".join(ch for ch in str(raw) if ch.isprintable()).strip()[:14]
        if not name:
            name = f"Player{self._next_pid}"
        taken = {q.name.lower() for q in self.players.values() if q.pid != keep_pid}
        base, i = name, 2
        while name.lower() in taken:
            name = f"{base[:12]}·{i}"
            i += 1
        return name

    def _join(self, conn, msg):
        pw = str(msg.get("pw", ""))
        is_host = bool(self.cfg["host_password"]) and pw == self.cfg["host_password"]
        if not is_host and self.cfg["join_password"] and pw != self.cfg["join_password"]:
            self._send_conn(conn, {"t": "err", "msg": "Wrong password."})
            return
        if conn.player:   # duplicate join on same socket -> re-ack
            self._ack_join(conn.player)
            return

        # session reclaim: reload/wifi blip keeps your seat and score
        sess = msg.get("sess")
        p = None
        if sess:
            for q in self.players.values():
                if q.session == sess and not q.is_bot:
                    p = q
                    break
        if p:
            if p.conn and p.conn is not conn:
                p.conn.player = None
                p.conn.close()
            new_name = msg.get("name")
            if new_name:
                p.name = self._sanitize_name(new_name, keep_pid=p.pid)
        else:
            if len(self._humans()) >= self.cfg["max_players"]:
                self._send_conn(conn, {"t": "err", "msg": "Server is full."})
                return
            pid = self._next_pid
            self._next_pid += 1
            p = Player(pid, self._sanitize_name(msg.get("name", "")),
                       COLORS[(pid - 1) % len(COLORS)])
            self.players[pid] = p

        p.conn = conn
        p.gone_since = None
        if is_host:
            p.is_host = True
        conn.player = p
        self.log(f"[gn] joined: {p.name}{' (HOST)' if p.is_host else ''} from {conn.remote}")
        self._ack_join(p)
        self.bcast_room()

    def _ack_join(self, p):
        self._send(p, {"t": "joined",
                       "you": {"id": p.pid, "name": p.name, "host": p.is_host},
                       "sess": p.session})
        if self.state != "lobby" and self.game:
            self._send(p, self._round_msg(joining=True))
            if p.conn:
                p.conn.send_state(json.dumps(
                    {"t": "s", **self.game.snapshot(full=True)}, separators=(",", ":")))

    def _send_conn(self, conn, obj):
        conn.send_text(json.dumps(obj, separators=(",", ":")))

    # =============================================================== messages

    def on_ws_message(self, conn, text):
        try:
            msg = json.loads(text)
            t = msg.get("t")
        except Exception:
            return
        try:
            if t == "join":
                self._join(conn, msg)
            elif t == "input":
                self._input(conn, msg)
            elif t == "host":
                self._host_action(conn, msg)
            elif t == "ping":
                self._send_conn(conn, {"t": "pong", "ts": msg.get("ts", 0)})
        except Exception:
            traceback.print_exc()

    def _input(self, conn, msg):
        p = conn.player
        if not p:
            return
        now = self.loop.time()
        if now - p.last_input < 0.01:   # rate limit
            return
        p.last_input = now
        k = msg.get("k") or {}
        p.keys = {key: bool(k.get(key)) for key in ("u", "d", "l", "r", "a")}
        if self.state == "playing" and self.game and p.in_round:
            self.game.on_input(p.pid, p.keys)

    def _host_action(self, conn, msg):
        p = conn.player
        if not p or not p.is_host:
            return
        a = msg.get("a")
        if a == "start":
            err = self.start_round(by=p.name)
            if err:
                self._send(p, {"t": "toast", "msg": err})
        elif a == "pause":
            self.toggle_pause(msg.get("v"))
        elif a == "abort":
            self.abort_round()
        elif a == "lobby":
            if self.state == "results":
                self.to_lobby()
        elif a == "set_game":
            self.set_game(msg.get("g"))
        elif a == "set":
            self.set_setting(msg.get("k"), msg.get("v"))
        elif a == "kick":
            target = self.players.get(msg.get("p"))
            if target:
                self.kick(target)
        elif a == "reset_scores":
            self.reset_scores()

    # =============================================================== settings

    def set_game(self, gid):
        if self.state not in ("lobby", "results"):
            self._toast("Finish the round before switching games.")
            return False
        if gid not in GAMES:
            return False
        self.game_id = gid
        self.cur_settings()
        self.log(f"[gn] game -> {GAMES[gid].NAME}")
        self.bcast_room()
        return True

    def set_setting(self, key, value):
        if self.state not in ("lobby", "results"):
            self._toast("Settings lock while a round is live.")
            return False
        schema = {s["k"]: s for s in GAMES[self.game_id].settings_schema()}
        s = schema.get(key)
        if not s:
            return False
        try:
            if s["type"] == "bool":
                if isinstance(value, str):
                    value = value.lower() in ("1", "true", "on", "yes")
                value = bool(value)
            elif s["type"] == "int":
                value = max(s["min"], min(s["max"], int(value)))
            elif s["type"] == "choice":
                if value not in s["choices"]:
                    try:  # console sends strings; choices may be ints
                        value = int(value)
                    except (TypeError, ValueError):
                        pass
                if value not in s["choices"]:
                    return False
        except (TypeError, ValueError):
            return False
        self.cur_settings()[key] = value
        if key == "bots":
            self._sync_bots()
        self.log(f"[gn] setting {key} = {value}")
        self.bcast_room()
        return True

    def _sync_bots(self):
        want = int(self.cur_settings().get("bots", 0))
        bots = [p for p in self.players.values() if p.is_bot]
        while len(bots) > want:
            b = bots.pop()
            self.players.pop(b.pid, None)
        used = {p.name for p in self.players.values()}
        pool = [n for n in BOT_NAMES if n not in used]
        random.shuffle(pool)
        while len(bots) < want:
            pid = self._next_pid
            self._next_pid += 1
            name = pool.pop(0) if pool else f"Bot{pid}"
            b = Player(pid, name, COLORS[(pid - 1) % len(COLORS)], is_bot=True)
            self.players[pid] = b
            bots.append(b)

    def reset_scores(self):
        for p in self.players.values():
            p.wins = 0
            p.points = 0
        self._toast("Scoreboard reset.")
        self.bcast_room()

    def kick(self, p):
        if p.is_bot:
            n = max(0, int(self.cur_settings().get("bots", 0)) - 1)
            self.cur_settings()["bots"] = n
        if self.game and p.in_round and self.state in ("countdown", "playing"):
            self.game.drop_player(p.pid)
        if p.conn:
            self._send(p, {"t": "kicked", "msg": "You were kicked by the host."})
            p.conn.player = None
            p.conn.close()
            p.conn = None
        self.players.pop(p.pid, None)
        self.log(f"[gn] kicked: {p.name}")
        self.bcast_room()

    # ================================================================= rounds

    def start_round(self, by="host"):
        """Begin a round. Returns None on success, else the reason it can't."""
        if self.state in ("countdown", "playing"):
            return "a round is already running — abort it first"
        self._sync_bots()
        participants = [p for p in self.players.values() if p.connected]
        cls = GAMES[self.game_id]
        if len(participants) < cls.MIN_PLAYERS:
            need = cls.MIN_PLAYERS
            return (f"need at least {need} connected player{'s' if need != 1 else ''} — "
                    f"open the join URL in a browser and/or add bots")
        for p in self.players.values():
            p.in_round = p in participants
            if not p.is_bot:
                p.keys = NO_KEYS.copy()
        roster = [{"pid": p.pid, "name": p.name, "bot": p.is_bot} for p in participants]
        self.round_no += 1
        self.game = cls(roster, dict(self.cur_settings()), random.Random())
        self.paused = False
        self.state = "countdown"
        self._phase_end = self.loop.time() + COUNTDOWN
        self.log(f"[gn] round {self.round_no}: {cls.NAME} with "
                 f"{len(roster)} players (started by {by})")
        self._bcast(self._round_msg())
        return None

    def _round_msg(self, joining=False):
        cls = GAMES[self.game_id]
        msg = {"t": "round", "phase": self.state, "round": self.round_no,
               "game": {"id": cls.ID, "name": cls.NAME, "tag": cls.TAG,
                        "controls": cls.CONTROLS},
               "settings": dict(self.cur_settings()),
               "arena": self.game.setup(),
               "roster": [p["pid"] for p in self.game.roster]}
        if self.state == "countdown":
            msg["secs"] = max(0.0, round(self._phase_end - self.loop.time(), 2))
        msg["paused"] = self.paused
        msg["preview"] = {"t": "s", **self.game.snapshot(full=True)}
        if joining:
            msg["spectate"] = True
        return msg

    def _finish_round(self):
        placements = self.game.placements()
        total = len(placements)
        rows = []
        winners = []
        for pid, place in placements:
            pts = total - place + 1
            p = self.players.get(pid)
            name = p.name if p else "?"
            if p:
                p.points += pts
                if place == 1:
                    p.wins += 1
            if place == 1:
                winners.append(pid)
            rows.append([pid, place, pts])
        self.state = "results"
        self._phase_end = self.loop.time() + RESULTS_SECS
        names = ", ".join(self.players[w].name for w in winners if w in self.players)
        self.log(f"[gn] round {self.round_no} over — winner: {names or '—'}")
        for pid, place, pts in rows:
            p = self.players.get(pid)
            if p:
                self.log(f"[gn]   #{place} {p.name}  (+{pts} pts, total {p.points})")
        self._bcast({"t": "end", "placements": rows,
                     "totals": [[p.pid, p.wins, p.points] for p in self.players.values()],
                     "winner": winners, "auto": int(RESULTS_SECS)})

    def to_lobby(self):
        self.state = "lobby"
        self.game = None
        self.paused = False
        now = self.loop.time()
        for p in list(self.players.values()):
            p.in_round = False
            if (not p.is_bot and p.conn is None and p.gone_since
                    and now - p.gone_since > PURGE_AFTER):
                self.players.pop(p.pid, None)
        self.bcast_room()

    def abort_round(self):
        if self.state in ("countdown", "playing"):
            self.log("[gn] round aborted")
            self._toast("Round aborted by host.")
            self.to_lobby()

    def toggle_pause(self, on=None):
        """Freeze/unfreeze the whole round (host pause menu / terminal)."""
        if self.state not in ("countdown", "playing"):
            return False
        want = (not self.paused) if on is None else bool(on)
        if want != self.paused:
            self.paused = want
            self.log(f"[gn] round {'paused' if want else 'resumed'}")
            self._bcast({"t": "pause", "on": want})
        return True

    # ================================================================== loop

    async def run(self):
        period = 1.0 / self.cfg["tick_rate"]
        next_t = self.loop.time()
        while True:
            try:
                self._step(period)
            except Exception:
                traceback.print_exc()
            next_t += period
            delay = next_t - self.loop.time()
            if delay < -1.0:        # machine slept / massive stall: resync
                next_t = self.loop.time()
                delay = 0.0
            await asyncio.sleep(max(0.0, delay))

    def _step(self, dt):
        now = self.loop.time()

        # keepalive: ping everyone, drop the silent
        if now - self._last_ka > KEEPALIVE:
            self._last_ka = now
            for p in list(self.players.values()):
                if p.conn:
                    if now - p.conn.last_rx > DEAD_AFTER:
                        p.conn.close()
                    else:
                        p.conn.ping()

        if self.state == "countdown":
            if self.paused:
                self._phase_end += dt          # freeze the countdown clock
            elif now >= self._phase_end:
                self.state = "playing"
                self._bcast({"t": "go"})
        elif self.state == "playing":
            if self.paused:
                return
            game = self.game
            if not game:
                self.state = "lobby"
                return
            skill = self.cur_settings().get("bot_skill", "normal")
            for p in self.players.values():
                if p.is_bot and p.in_round:
                    game.on_input(p.pid, game.bot_input(p.pid, skill))
            events = []
            game.tick(dt, events)
            if events:
                self._bcast({"t": "fx", "ev": events})
            self._tick_no += 1
            if self._tick_no % self._snap_every == 0:
                self._bcast_state({"t": "s", **game.snapshot()})
            if game.is_over():
                self._finish_round()
        elif self.state == "results":
            if now >= self._phase_end:
                self.to_lobby()

    # ================================================================ console

    def console(self, line):
        """Terminal host commands. Returns a reply string."""
        parts = line.strip().split()
        if not parts:
            return ""
        cmd, args = parts[0].lower(), parts[1:]
        if cmd == "help":
            return ("commands: start | pause | resume | lobby | abort | game <id>\n"
                    "  set <key> <value> | settings | players | scores | resetscores\n"
                    "  bots <n> | skill <easy|normal|mean> | kick <name> | say <msg> | quit")
        if cmd == "start":
            err = self.start_round(by="terminal")
            return err or "round starting…"
        if cmd == "lobby":
            self.to_lobby()
            return "back to lobby"
        if cmd == "abort":
            self.abort_round()
            return "aborted"
        if cmd in ("pause", "resume"):
            ok = self.toggle_pause(cmd == "pause")
            return ("paused" if self.paused else "resumed") if ok else "no round running"
        if cmd == "game":
            if not args:
                return "games: " + ", ".join(GAMES)
            return "ok" if self.set_game(args[0]) else f"unknown game (have: {', '.join(GAMES)})"
        if cmd == "set":
            if len(args) < 2:
                return "usage: set <key> <value>"
            val = " ".join(args[1:])
            try:
                val = json.loads(val)
            except Exception:
                pass
            return "ok" if self.set_setting(args[0], val) else "bad key or value"
        if cmd == "bots":
            return "ok" if args and self.set_setting("bots", args[0]) else "usage: bots <0-6>"
        if cmd == "skill":
            return "ok" if args and self.set_setting("bot_skill", args[0]) else "usage: skill easy|normal|mean"
        if cmd == "settings":
            s = self.cur_settings()
            schema = GAMES[self.game_id].settings_schema()
            lines = [f"{GAMES[self.game_id].NAME}:"]
            for item in schema:
                lines.append(f"  {item['k']} = {s[item['k']]}")
            return "\n".join(lines)
        if cmd == "players":
            if not self.players:
                return "nobody here yet"
            return "\n".join(
                f"  {p.name:<16} {'BOT' if p.is_bot else ('online' if p.conn else 'offline')}"
                f"{'  HOST' if p.is_host else ''}" for p in self.players.values())
        if cmd == "scores":
            rows = sorted(self.players.values(), key=lambda p: (-p.wins, -p.points))
            return "\n".join(f"  {p.name:<16} {p.wins} wins  {p.points} pts" for p in rows) or "no scores"
        if cmd == "resetscores":
            self.reset_scores()
            return "scores reset"
        if cmd == "kick":
            if not args:
                return "usage: kick <name>"
            name = " ".join(args).lower()
            for p in list(self.players.values()):
                if p.name.lower() == name:
                    self.kick(p)
                    return f"kicked {p.name}"
            return "no such player"
        if cmd == "say":
            self._toast("HOST: " + " ".join(args))
            return "sent"
        if cmd in ("quit", "exit"):
            raise SystemExit
        return f"unknown command: {cmd} (try 'help')"
