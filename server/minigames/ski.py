"""
ski.py — AVALANCHE RUN.

Black-diamond descent: the mountain scrolls under everyone (one shared,
ever-accelerating camera — parallax heaven client-side), an avalanche chews
the top of the screen, trees and rocks want your teeth, and your friends are
throwing snowballs. Get bonked, tumble, get swallowed. Last skier riding wins.

Obstacles are procedurally spawned ahead of the camera and synced as deltas
(each obstacle is sent exactly once), so bandwidth stays flat forever.
"""

import math

from .base import MiniGame, BOT_SETTINGS, WORLD_W, WORLD_H

SPAWN_AHEAD = 900        # generate terrain this far below the camera edge
KILL_MARGIN = 26         # avalanche line: this far below the top of the screen
TUMBLE_SECS = 1.05
BALL_SPEED = 380.0
BALL_LIFE = 1.4


class AvalancheRun(MiniGame):
    ID = "ski"
    NAME = "Avalanche Run"
    TAG = "Ski the endless black diamond. Dodge trees, snowball your friends, outrun the avalanche."
    CONTROLS = "A/D steer · W brake · S tuck (speed) · SPACE snowball"
    MIN_PLAYERS = 1

    @classmethod
    def settings_schema(cls):
        return [
            {"k": "round_time", "label": "Round cap (s)", "type": "choice",
             "def": 120, "choices": [60, 90, 120, 180]},
            {"k": "ramp", "label": "Mountain", "type": "choice",
             "def": "normal", "choices": ["chill", "normal", "wild"]},
            {"k": "obstacles", "label": "Obstacles", "type": "choice",
             "def": "normal", "choices": ["sparse", "normal", "forest"]},
            {"k": "snowballs", "label": "Snowballs", "type": "choice",
             "def": "normal", "choices": ["off", "normal", "rapid"]},
            {"k": "ice", "label": "Icy skis", "type": "bool", "def": False},
            {"k": "reverse", "label": "Reverse controls", "type": "bool", "def": False},
            *BOT_SETTINGS,
        ]

    def __init__(self, roster, settings, rng):
        super().__init__(roster, settings, rng)
        s = settings
        self.p = {
            "radius": 12.0,
            "accel": 950.0, "drag": 5.0 if not s.get("ice") else 1.6,
            "vmax": 260.0 if not s.get("ice") else 300.0,
            "ramp": {"chill": 0.012, "normal": 0.022, "wild": 0.040}[s.get("ramp", "normal")],
            "ball_cd": {"off": 0.0, "normal": 2.2, "rapid": 1.1}.get(s.get("snowballs", "normal"), 2.2),
            "cap": float(s.get("round_time", 120)),
            "reverse": -1.0 if s.get("reverse") else 1.0,
        }
        gap = {"sparse": (95, 160, 1), "normal": (70, 120, 2),
               "forest": (52, 92, 3)}[s.get("obstacles", "normal")]
        self._gap_min, self._gap_max, self._per_row = gap

        self.cam = 0.0            # world-y of the top of the screen
        self.spd = 150.0          # downhill scroll speed (ramps up)
        self.t = 0.0
        self.order = []
        self._over = False

        n = max(1, len(roster))
        self.ent = {}
        for i, pl in enumerate(roster):
            self.ent[pl["pid"]] = {
                "x": WORLD_W * (i + 1) / (n + 1), "y": 200.0,
                "vx": 0.0, "alive": True, "keys": _NO_KEYS.copy(),
                "prev_a": False, "cd": 0.0, "tumble": 0.0,
            }

        self.obstacles = {}       # id -> [x, y, type]  (0 tree, 1 rock)
        self._next_oid = 1
        self._new_obs = []        # deltas since last snapshot
        self._spawn_y = 620.0
        self.balls = []           # {id, x, y, vx, vy, life, owner}
        self._next_bid = 1
        self._gen_terrain()

    # ---------------------------------------------------------------- terrain

    def _gen_terrain(self):
        while self._spawn_y < self.cam + SPAWN_AHEAD:
            for _ in range(self.rng.randint(1, self._per_row)):
                oid = self._next_oid
                self._next_oid += 1
                kind = 0 if self.rng.random() < 0.62 else 1
                ob = [round(self.rng.uniform(28, WORLD_W - 28), 1),
                      round(self._spawn_y + self.rng.uniform(-20, 20), 1), kind]
                self.obstacles[oid] = ob
                self._new_obs.append([oid, *ob])
            self._spawn_y += self.rng.uniform(self._gap_min, self._gap_max)
        # cull far behind the avalanche
        gone = [oid for oid, ob in self.obstacles.items() if ob[1] < self.cam - 120]
        for oid in gone:
            del self.obstacles[oid]

    # ------------------------------------------------------------------ input

    def on_input(self, pid, keys):
        e = self.ent.get(pid)
        if e and e["alive"]:
            e["keys"] = keys

    # ------------------------------------------------------------------- tick

    def tick(self, dt, events):
        if self._over:
            return
        p = self.p
        self.t += dt
        self.spd = min(620.0, self.spd * (1.0 + p["ramp"] * dt))
        self.cam += self.spd * dt
        self._gen_terrain()

        alive = [(pid, e) for pid, e in self.ent.items() if e["alive"]]

        for pid, e in alive:
            k = e["keys"]
            tumbling = e["tumble"] > 0
            e["tumble"] = max(0.0, e["tumble"] - dt)
            ix = ((1.0 if k["r"] else 0.0) - (1.0 if k["l"] else 0.0)) * p["reverse"]
            iy = ((1.0 if k["d"] else 0.0) - (1.0 if k["u"] else 0.0)) * p["reverse"]
            if tumbling:
                ix = iy = 0.0
            # lateral steering
            e["vx"] += ix * p["accel"] * dt
            e["vx"] *= math.exp(-p["drag"] * dt)
            e["vx"] = max(-p["vmax"], min(p["vmax"], e["vx"]))
            e["x"] += e["vx"] * dt
            if e["x"] < 14:
                e["x"], e["vx"] = 14, abs(e["vx"]) * 0.5
            elif e["x"] > WORLD_W - 14:
                e["x"], e["vx"] = WORLD_W - 14, -abs(e["vx"]) * 0.5
            # downhill: ride with the camera, tuck to gain, brake to lose
            slide = self.spd * (0.55 if tumbling else 1.0 + iy * 0.24)
            e["y"] += slide * dt
            e["y"] = min(e["y"], self.cam + WORLD_H - 30)   # can't outrun the screen
            # snowball throw
            if (not tumbling and p["ball_cd"] > 0 and k["a"] and not e["prev_a"]
                    and e["cd"] <= 0):
                dx, dy = ix, iy
                if dx == 0 and dy == 0:
                    dy = 1.0                                 # default: chuck it downhill
                mag = math.hypot(dx, dy)
                self.balls.append({"id": self._next_bid, "x": e["x"], "y": e["y"],
                                   "owner": pid,
                                   "vx": dx / mag * BALL_SPEED,
                                   "vy": dy / mag * BALL_SPEED + self.spd,
                                   "life": BALL_LIFE})
                self._next_bid += 1
                e["cd"] = p["ball_cd"]
                events.append(["throw", pid])
            e["prev_a"] = k["a"]
            e["cd"] = max(0.0, e["cd"] - dt)

            # obstacle collisions (only while not already tumbling)
            if not tumbling:
                for ob in self.obstacles.values():
                    if abs(ob[1] - e["y"]) > 30:
                        continue
                    orad = 12 if ob[2] == 0 else 14
                    if math.hypot(ob[0] - e["x"], ob[1] - e["y"]) < orad + p["radius"]:
                        e["tumble"] = TUMBLE_SECS
                        e["y"] -= 34
                        e["vx"] = -e["vx"] * 0.5 + self.rng.uniform(-60, 60)
                        events.append(["bonk", pid, round(e["x"], 1), round(e["y"] - self.cam, 1)])
                        break

        # light player-vs-player jostling
        for i in range(len(alive)):
            for j in range(i + 1, len(alive)):
                a, b = alive[i][1], alive[j][1]
                dx, dy = b["x"] - a["x"], b["y"] - a["y"]
                dist = math.hypot(dx, dy)
                rr = p["radius"] * 2
                if 0 < dist < rr:
                    nx, ny = dx / dist, dy / dist
                    push = (rr - dist) / 2
                    a["x"] -= nx * push
                    b["x"] += nx * push
                    a["vx"] -= nx * 90
                    b["vx"] += nx * 90

        # snowballs fly, splat, expire
        keep = []
        for ball in self.balls:
            ball["x"] += ball["vx"] * dt
            ball["y"] += ball["vy"] * dt
            ball["life"] -= dt
            hit = False
            if ball["life"] > 0 and 0 < ball["x"] < WORLD_W:
                for pid, e in alive:
                    if pid == ball["owner"] or not e["alive"] or e["tumble"] > 0:
                        continue
                    if math.hypot(e["x"] - ball["x"], e["y"] - ball["y"]) < p["radius"] + 5:
                        e["tumble"] = TUMBLE_SECS
                        e["y"] -= 46
                        e["vx"] += ball["vx"] * 0.4
                        events.append(["splat", pid, ball["owner"]])
                        hit = True
                        break
                if not hit:
                    keep.append(ball)
        self.balls = keep

        # the avalanche takes the slow (same tick = tie)
        kill_y = self.cam + KILL_MARGIN
        wiped = []
        for pid, e in alive:
            if e["y"] < kill_y and e["alive"]:
                e["alive"] = False
                wiped.append(pid)
                events.append(["wipe", pid, round(e["x"], 1), round(e["y"] - self.cam, 1)])
        if wiped:
            self.order.append(wiped)

        n_alive = sum(1 for e in self.ent.values() if e["alive"])
        if (len(self.ent) > 1 and n_alive <= 1) or (len(self.ent) == 1 and n_alive == 0):
            self._over = True
        elif self.t >= p["cap"]:
            self._over = True

    # ------------------------------------------------------------- snapshots

    def snapshot(self, full=False):
        if full:
            obs = [[oid, *ob] for oid, ob in self.obstacles.items()]
        else:
            obs, self._new_obs = self._new_obs, []
        return {"g": self.ID, "cam": round(self.cam, 1), "spd": round(self.spd),
                "e": [[pid, round(e["x"], 1), round(e["y"], 1),
                       1 if e["alive"] else 0,
                       round(min(1.0, 1.0 - e["cd"] / max(0.01, self.p["ball_cd"] or 1)), 2),
                       round(e["tumble"] / TUMBLE_SECS, 2)]
                      for pid, e in self.ent.items()],
                "obs": obs,
                "balls": [[b["id"], round(b["x"], 1), round(b["y"], 1),
                           round(b["vx"]), round(b["vy"])] for b in self.balls]}

    def setup(self):
        out = {"g": self.ID, "w": WORLD_W, "h": WORLD_H}
        if self.p["ball_cd"] > 0:
            out["action"] = "SNOWBALL"
        return out

    def status(self):
        alive = sum(1 for e in self.ent.values() if e["alive"])
        return f"{alive} skiing · {int(self.spd)} px/s · {int(self.cam / 100)}0m"

    def drop_player(self, pid):
        e = self.ent.get(pid)
        if e and e["alive"]:
            e["alive"] = False
            self.order.insert(0, [pid])
            n_alive = sum(1 for q in self.ent.values() if q["alive"])
            if len(self.ent) > 1 and n_alive <= 1:
                self._over = True

    def is_over(self):
        return self._over

    def placements(self):
        groups = list(self.order)
        survivors = [pid for pid, e in self.ent.items() if e["alive"]]
        if survivors:
            groups.append(survivors)
        return self._rank(groups)

    def _rank(self, groups):
        res, better = [], 0
        for grp in reversed(groups):
            for pid in grp:
                res.append((pid, better + 1))
            better += len(grp)
        res.sort(key=lambda t: t[1])
        return res

    # ------------------------------------------------------------------ bots

    def bot_input(self, pid, skill):
        me = self.ent.get(pid)
        keys = _NO_KEYS.copy()
        if not me or not me["alive"] or me["tumble"] > 0:
            return keys
        cfg = {"easy": (46, 0.010, 90), "normal": (64, 0.028, 60),
               "mean": (80, 0.05, 35)}[skill]
        dodge_w, throw_p, jitter = cfg
        rng = self.rng

        # dodge: strongest repulsion from the nearest obstacle just ahead
        steer = 0.0
        for ob in self.obstacles.values():
            dy = ob[1] - me["y"]
            if 10 < dy < 170 and abs(ob[0] - me["x"]) < dodge_w:
                w = (170 - dy) / 170
                steer -= math.copysign(w, ob[0] - me["x"])
        if steer == 0:
            steer = (WORLD_W / 2 - me["x"]) / 400 + rng.uniform(-0.4, 0.4)
        steer += rng.uniform(-jitter, jitter) / 100
        keys["l"], keys["r"] = steer < -0.12, steer > 0.12

        # hold position in the middle band of the screen
        band = me["y"] - self.cam
        if band < 250:
            keys["d"] = True
        elif band > 430:
            keys["u"] = True

        # opportunistic snowball at whoever is nearby
        if me["cd"] <= 0 and self.p["ball_cd"] > 0 and rng.random() < throw_p:
            for opid, e in self.ent.items():
                if opid != pid and e["alive"] and abs(e["y"] - me["y"]) < 220:
                    keys["a"] = True
                    if abs(e["x"] - me["x"]) > 40:
                        keys["l"], keys["r"] = e["x"] < me["x"], e["x"] > me["x"]
                    keys["d"] = e["y"] > me["y"]
                    keys["u"] = e["y"] < me["y"]
                    break
        return keys


_NO_KEYS = {"u": False, "d": False, "l": False, "r": False, "a": False}
