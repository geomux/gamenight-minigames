"""
sumo.py — SUMO RING.

Top-down circular platform over the void. Bump friends off; last one on the
ring wins. The ring shrinks over time so rounds always resolve. Dash (space)
for big knockback plays. Every "modifier" is just a lobby setting that tweaks
the params dict below — that's the whole modifier engine.
"""

import math

from .base import MiniGame, BOT_SETTINGS, WORLD_W, WORLD_H

CX, CY = WORLD_W / 2, WORLD_H / 2
R0 = 232.0          # starting ring radius
R_MIN = 26.0        # radius the ring shrinks down to
GRACE = 5.0         # seconds before shrinking starts
DASH_WINDOW = 0.35  # seconds after a dash during which bumps hit harder


class SumoRing(MiniGame):
    ID = "sumo"
    NAME = "Sumo Ring"
    TAG = "Bump your friends off a shrinking ring. Last one standing wins."
    CONTROLS = "WASD / arrows to move · SPACE to dash"
    MIN_PLAYERS = 1

    @classmethod
    def settings_schema(cls):
        return [
            {"k": "round_time", "label": "Round cap (s)", "type": "choice",
             "def": 90, "choices": [30, 60, 90, 120, 180]},
            {"k": "shrink", "label": "Ring shrink", "type": "choice",
             "def": "normal", "choices": ["off", "slow", "normal", "fast"]},
            {"k": "speed", "label": "Speed", "type": "choice",
             "def": "normal", "choices": ["chill", "normal", "turbo"]},
            {"k": "ice", "label": "Ice floor", "type": "bool", "def": False},
            {"k": "reverse", "label": "Reverse controls", "type": "bool", "def": False},
            {"k": "bodies", "label": "Body size", "type": "choice",
             "def": "normal", "choices": ["normal", "giants", "tinies", "chaos"]},
            {"k": "dash", "label": "Dash (space)", "type": "choice",
             "def": "normal", "choices": ["off", "normal", "mega"]},
            {"k": "wind", "label": "Wind", "type": "bool", "def": False},
            *BOT_SETTINGS,
        ]

    def __init__(self, roster, settings, rng):
        super().__init__(roster, settings, rng)
        s = settings

        # ---- params: every setting lands here as a plain number ----
        p = {
            "radius": 13.0, "accel": 1150.0, "drag": 4.0, "vmax": 305.0,
            "bump_e": 0.35, "bump_boost": 210.0,
            "dash_v": 430.0, "dash_cd": 2.5,
            "wind_a": 55.0 if s.get("wind") else 0.0,
            "cap": float(s.get("round_time", 90)),
            "reverse": -1.0 if s.get("reverse") else 1.0,
        }
        if s.get("speed") == "chill":
            p["accel"] *= 0.72; p["vmax"] *= 0.75
        elif s.get("speed") == "turbo":
            p["accel"] *= 1.32; p["vmax"] *= 1.30; p["drag"] *= 0.92
        if s.get("ice"):
            p["drag"] = 0.9; p["accel"] *= 0.62
        if s.get("dash") == "off":
            p["dash_v"] = 0.0
        elif s.get("dash") == "mega":
            p["dash_v"] = 590.0; p["dash_cd"] = 3.0
        # shrink profile: ring reaches R_MIN at `shrink_end` seconds
        shrink = s.get("shrink", "normal")
        p["shrink_end"] = {"off": 0.0, "slow": p["cap"] * 1.1,
                           "normal": p["cap"] * 0.8, "fast": p["cap"] * 0.5}[shrink]
        self.p = p

        # ---- per-player bodies, spawned on an inner circle ----
        bodies = s.get("bodies", "normal")
        n = max(1, len(roster))
        self.ent = {}
        for i, pl in enumerate(roster):
            r = p["radius"]
            if bodies == "giants":
                r = 21.0
            elif bodies == "tinies":
                r = 9.0
            elif bodies == "chaos":
                r = rng.uniform(9.0, 22.0)
            ang = 2 * math.pi * i / n
            d = R0 * 0.62
            self.ent[pl["pid"]] = {
                "x": CX + d * math.cos(ang), "y": CY + d * math.sin(ang),
                "vx": 0.0, "vy": 0.0, "r": r, "alive": True,
                "keys": _NO_KEYS.copy(), "prev_a": False,
                "cd": 0.0, "dash_t": 99.0,  # time since last dash
            }

        self.t = 0.0
        self.R = R0
        self.order = []            # fall order: list of tie-groups, earliest first
        self._wind_ang = rng.uniform(0, 2 * math.pi)
        self._bot_state = {}       # pid -> wander phase etc.
        self._over = False

    # ------------------------------------------------------------------ setup

    def setup(self):
        return {"g": self.ID, "w": WORLD_W, "h": WORLD_H,
                "cx": CX, "cy": CY, "R0": R0,
                "wind": bool(self.p["wind_a"])}

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

        # ring radius
        if p["shrink_end"] > 0 and self.t > GRACE:
            frac = min(1.0, (self.t - GRACE) / max(0.1, p["shrink_end"] - GRACE))
            self.R = R0 + (R_MIN - R0) * frac
        # wind drifts around
        if p["wind_a"]:
            self._wind_ang += self.rng.uniform(-1.0, 1.0) * 0.7 * dt
        wx = math.cos(self._wind_ang) * p["wind_a"]
        wy = math.sin(self._wind_ang) * p["wind_a"]

        alive = [(pid, e) for pid, e in self.ent.items() if e["alive"]]

        # --- integrate inputs / velocities ---
        for pid, e in alive:
            k = e["keys"]
            ix = (1.0 if k["r"] else 0.0) - (1.0 if k["l"] else 0.0)
            iy = (1.0 if k["d"] else 0.0) - (1.0 if k["u"] else 0.0)
            ix *= p["reverse"]; iy *= p["reverse"]
            mag = math.hypot(ix, iy)
            if mag > 0:
                ix /= mag; iy /= mag
            e["vx"] += (ix * p["accel"] + wx) * dt
            e["vy"] += (iy * p["accel"] + wy) * dt
            # dash: edge-triggered on the action key
            if k["a"] and not e["prev_a"] and e["cd"] <= 0 and p["dash_v"] > 0:
                dx, dy = ix, iy
                if dx == 0 and dy == 0:  # no input held: dash along motion
                    s = math.hypot(e["vx"], e["vy"])
                    if s > 10:
                        dx, dy = e["vx"] / s, e["vy"] / s
                if dx or dy:
                    e["vx"] += dx * p["dash_v"]
                    e["vy"] += dy * p["dash_v"]
                    e["cd"] = p["dash_cd"]
                    e["dash_t"] = 0.0
                    events.append(["dash", pid])
            e["prev_a"] = k["a"]
            e["cd"] = max(0.0, e["cd"] - dt)
            e["dash_t"] += dt
            # drag + speed clamp
            f = math.exp(-p["drag"] * dt)
            e["vx"] *= f; e["vy"] *= f
            s = math.hypot(e["vx"], e["vy"])
            lim = p["vmax"] * (2.2 if e["dash_t"] < DASH_WINDOW else 1.0)
            if s > lim:
                e["vx"] *= lim / s; e["vy"] *= lim / s
            e["x"] += e["vx"] * dt
            e["y"] += e["vy"] * dt

        # --- collisions: equal-mass bumps with a knockback boost ---
        for i in range(len(alive)):
            for j in range(i + 1, len(alive)):
                a, b = alive[i][1], alive[j][1]
                dx = b["x"] - a["x"]; dy = b["y"] - a["y"]
                dist = math.hypot(dx, dy)
                rr = a["r"] + b["r"]
                if dist >= rr or dist == 0:
                    continue
                nx, ny = dx / dist, dy / dist
                # positional separation
                push = (rr - dist) / 2
                a["x"] -= nx * push; a["y"] -= ny * push
                b["x"] += nx * push; b["y"] += ny * push
                # impulse along the normal
                rvn = (b["vx"] - a["vx"]) * nx + (b["vy"] - a["vy"]) * ny
                if rvn < 0:
                    boost = p["bump_boost"]
                    if a["dash_t"] < DASH_WINDOW or b["dash_t"] < DASH_WINDOW:
                        boost *= 1.8
                    imp = -(1 + p["bump_e"]) * rvn / 2 + boost
                    a["vx"] -= nx * imp; a["vy"] -= ny * imp
                    b["vx"] += nx * imp; b["vy"] += ny * imp
                    if imp > 120:
                        events.append(["hit",
                                       round((a["x"] + b["x"]) / 2, 1),
                                       round((a["y"] + b["y"]) / 2, 1),
                                       min(1.0, round(imp / 500, 2))])

        # --- falls (checked together so same-tick falls tie) ---
        fallen = []
        for pid, e in alive:
            d = math.hypot(e["x"] - CX, e["y"] - CY)
            if d > self.R + e["r"] * 0.35:
                e["alive"] = False
                fallen.append(pid)
                events.append(["fall", pid, round(e["x"], 1), round(e["y"], 1),
                               round(e["vx"], 1), round(e["vy"], 1)])
        if fallen:
            self.order.append(fallen)

        # --- end conditions ---
        n_alive = sum(1 for e in self.ent.values() if e["alive"])
        if len(self.ent) > 1 and n_alive <= 1:
            self._over = True
        elif len(self.ent) == 1 and n_alive == 0:
            self._over = True
        elif self.t >= p["cap"]:
            self._over = True

    # ---------------------------------------------------------------- boilerplate

    def snapshot(self, full=False):
        out = {"g": self.ID, "R": round(self.R, 1),
               "e": [[pid, round(e["x"], 1), round(e["y"], 1),
                      1 if e["alive"] else 0,
                      round(min(1.0, 1.0 - e["cd"] / max(0.01, self.p["dash_cd"])), 2),
                      round(e["r"], 1)]
                     for pid, e in self.ent.items()]}
        if self.p["wind_a"]:
            out["wind"] = [round(math.cos(self._wind_ang), 2),
                           round(math.sin(self._wind_ang), 2)]
        return out

    def status(self):
        alive = sum(1 for e in self.ent.values() if e["alive"])
        return f"{alive} on the ring · radius {int(self.R)} · {int(self.t)}s / {int(self.p['cap'])}s"

    def drop_player(self, pid):
        e = self.ent.get(pid)
        if e and e["alive"]:
            e["alive"] = False
            self.order.insert(0, [pid])  # abandoned ship: last place

    def is_over(self):
        return self._over

    def placements(self):
        groups = list(self.order)
        survivors = [pid for pid, e in self.ent.items() if e["alive"]]
        if survivors:
            groups.append(survivors)
        res = []
        placed_better = 0
        for grp in reversed(groups):           # winners first
            for pid in grp:
                res.append((pid, placed_better + 1))
            placed_better += len(grp)
        res.sort(key=lambda t: t[1])
        return res

    # ------------------------------------------------------------------- bots

    def bot_input(self, pid, skill):
        me = self.ent.get(pid)
        if not me or not me["alive"]:
            return _NO_KEYS.copy()
        st = self._bot_state.setdefault(pid, {"wander": self.rng.uniform(0, 6.28), "lapse": 0.0})
        cfg = _BOT_CFG[skill]
        rng = self.rng

        # occasional attention lapse (easy bots wander off)
        st["lapse"] = max(0.0, st["lapse"] - 1 / 30)
        if rng.random() < cfg["lapse"]:
            st["lapse"] = rng.uniform(0.3, 0.9)

        # steer: chase nearest living opponent
        tx, ty = CX, CY
        best = 1e9
        for opid, e in self.ent.items():
            if opid == pid or not e["alive"]:
                continue
            d = math.hypot(e["x"] - me["x"], e["y"] - me["y"])
            if d < best:
                best = d
                lead = cfg["lead"]
                tx, ty = e["x"] + e["vx"] * lead, e["y"] + e["vy"] * lead
        sx, sy = tx - me["x"], ty - me["y"]

        # edge care: overrides chasing when too close to the rim
        dc = math.hypot(me["x"] - CX, me["y"] - CY)
        if dc > self.R * cfg["edge"] and st["lapse"] <= 0:
            sx, sy = CX - me["x"], CY - me["y"]

        # wander + aim error
        st["wander"] += rng.uniform(-0.4, 0.4)
        err = cfg["err"]
        ang = math.atan2(sy, sx) + rng.uniform(-err, err) + math.sin(st["wander"]) * 0.2
        if st["lapse"] > 0:
            ang += rng.uniform(-2.5, 2.5)

        dx, dy = math.cos(ang), math.sin(ang)
        keys = {"u": dy < -0.38, "d": dy > 0.38, "l": dx < -0.38, "r": dx > 0.38, "a": False}
        # dash when lined up, close and off cooldown
        if (me["cd"] <= 0 and best < 70 + me["r"] * 2.5
                and rng.random() < cfg["dash"]):
            keys["a"] = True
        return keys


_NO_KEYS = {"u": False, "d": False, "l": False, "r": False, "a": False}

_BOT_CFG = {
    "easy":   {"edge": 0.66, "lapse": 0.010, "err": 0.65, "dash": 0.02, "lead": 0.00},
    "normal": {"edge": 0.70, "lapse": 0.002, "err": 0.30, "dash": 0.06, "lead": 0.12},
    "mean":   {"edge": 0.74, "lapse": 0.000, "err": 0.10, "dash": 0.14, "lead": 0.25},
}
