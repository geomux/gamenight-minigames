"""
planes.py — ACES HIGH.

Steampunk dogfight over the clouds. Your plane always flies forward; you
bank left/right, throttle or brake, and hold SPACE to fire. Edges wrap
Asteroids-style so the sky never ends. Lose all your hearts and you spiral
into the sunset. Mid-air collisions cost both pilots a heart — gloriously.

Last ace flying wins; at the time cap survivors rank by hearts left.
"""

import math

from .base import MiniGame, BOT_SETTINGS, WORLD_W, WORLD_H

PLANE_R = 11.0
BULLET_R = 3.0
BULLET_LIFE = 0.9
INVULN = 1.0             # seconds of blinking after taking a hit
TWO_PI = 2 * math.pi


class AcesHigh(MiniGame):
    ID = "planes"
    NAME = "Aces High"
    TAG = "Steampunk dogfight — bank, throttle, and shoot your friends out of the sky."
    CONTROLS = "A/D bank · W throttle · S brake · SPACE fire"
    MIN_PLAYERS = 1

    @classmethod
    def settings_schema(cls):
        return [
            {"k": "round_time", "label": "Round cap (s)", "type": "choice",
             "def": 90, "choices": [60, 90, 120, 180]},
            {"k": "lives", "label": "Hearts", "type": "int", "def": 3, "min": 1, "max": 5},
            {"k": "guns", "label": "Guns", "type": "choice",
             "def": "normal", "choices": ["pea shooter", "normal", "blaster"]},
            {"k": "speed", "label": "Plane speed", "type": "choice",
             "def": "normal", "choices": ["chill", "normal", "turbo"]},
            {"k": "turn", "label": "Turning", "type": "choice",
             "def": "normal", "choices": ["normal", "tight"]},
            {"k": "islands", "label": "Floating islands", "type": "choice",
             "def": "few", "choices": ["none", "few", "lots"]},
            {"k": "gusts", "label": "Wind gusts", "type": "choice",
             "def": "few", "choices": ["none", "few", "lots"]},
            {"k": "ram_damage", "label": "Ramming hurts", "type": "bool", "def": False},
            {"k": "reverse", "label": "Reverse controls", "type": "bool", "def": False},
            *BOT_SETTINGS,
        ]

    def __init__(self, roster, settings, rng):
        super().__init__(roster, settings, rng)
        s = settings
        guns = {"pea shooter": (1.1, 380.0), "normal": (0.8, 460.0),
                "blaster": (0.5, 540.0)}[s.get("guns", "normal")]
        self.p = {
            "speed": {"chill": 170.0, "normal": 210.0, "turbo": 260.0}[s.get("speed", "normal")],
            "turn": {"normal": 3.2, "tight": 4.4}[s.get("turn", "normal")],
            "fire_cd": guns[0], "bullet_v": guns[1],
            "lives": int(s.get("lives", 3)),
            "cap": float(s.get("round_time", 90)),
            "reverse": -1.0 if s.get("reverse") else 1.0,
            "ram": bool(s.get("ram_damage")),
        }
        n = max(1, len(roster))
        self.ent = {}
        for i, pl in enumerate(roster):
            ang = TWO_PI * i / n
            self.ent[pl["pid"]] = {
                "x": WORLD_W / 2 + math.cos(ang) * 340,
                "y": WORLD_H / 2 + math.sin(ang) * 175,
                "a": ang + math.pi / 2,          # heading, radians
                "hp": self.p["lives"], "alive": True,
                "keys": _NO_KEYS.copy(), "cd": 0.0, "inv": 0.0,
            }
        self.bullets = []        # {id,x,y,vx,vy,life,owner}
        self._next_bid = 1
        self.t = 0.0
        self.order = []
        self._over = False
        self._bot_state = {}     # pid -> {bias, until}: attack-run offsets

        # floating islands: solid cover — planes bounce, bullets stop
        n_isl = {"none": 0, "few": 3, "lots": 5}[s.get("islands", "few")]
        spawns = [(e["x"], e["y"]) for e in self.ent.values()]
        self.islands = []
        for _ in range(300):
            if len(self.islands) >= n_isl:
                break
            r = rng.uniform(30, 52)
            x = rng.uniform(r + 50, WORLD_W - r - 50)
            y = rng.uniform(r + 40, WORLD_H - r - 40)
            if any(math.hypot(x - ix, y - iy) < r + ir + 70 for ix, iy, ir in self.islands):
                continue
            if any(math.hypot(x - sx, y - sy) < r + 40 for sx, sy in spawns):
                continue
            self.islands.append((x, y, r))

        # gust zones: a directional shove — ride it for a boost
        n_gust = {"none": 0, "few": 2, "lots": 4}[s.get("gusts", "few")]
        self.gusts = []
        for _ in range(300):
            if len(self.gusts) >= n_gust:
                break
            r = rng.uniform(46, 64)
            x = rng.uniform(r + 20, WORLD_W - r - 20)
            y = rng.uniform(r + 20, WORLD_H - r - 20)
            if any(math.hypot(x - ix, y - iy) < r + ir + 30 for ix, iy, ir in self.islands):
                continue
            if any(math.hypot(x - gx, y - gy) < r + gr + 40 for gx, gy, gr, _ in self.gusts):
                continue
            self.gusts.append((x, y, r, rng.uniform(0, TWO_PI)))

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
        alive = [(pid, e) for pid, e in self.ent.items() if e["alive"]]

        # fly
        for pid, e in alive:
            k = e["keys"]
            turn = ((1.0 if k["r"] else 0.0) - (1.0 if k["l"] else 0.0)) * p["reverse"]
            e["a"] = (e["a"] + turn * p["turn"] * dt) % TWO_PI
            spd = p["speed"] * (1.28 if k["u"] else 0.72 if k["d"] else 1.0)
            e["x"] = (e["x"] + math.cos(e["a"]) * spd * dt) % WORLD_W
            e["y"] = (e["y"] + math.sin(e["a"]) * spd * dt) % WORLD_H
            # gusts shove you along their direction — ride them for a boost
            for gx, gy, gr, gang in self.gusts:
                if math.hypot(e["x"] - gx, e["y"] - gy) < gr:
                    e["x"] = (e["x"] + math.cos(gang) * 170 * dt) % WORLD_W
                    e["y"] = (e["y"] + math.sin(gang) * 170 * dt) % WORLD_H
            # islands are solid: push out and bounce the heading
            for ix, iy, ir in self.islands:
                ddx = e["x"] - ix
                ddy = e["y"] - iy
                d = math.hypot(ddx, ddy)
                rr = ir + PLANE_R
                if 0 < d < rr:
                    nx, ny = ddx / d, ddy / d
                    e["x"] = ix + nx * rr
                    e["y"] = iy + ny * rr
                    hx, hy = math.cos(e["a"]), math.sin(e["a"])
                    dot = hx * nx + hy * ny
                    if dot < 0:                    # flying into it: reflect
                        e["a"] = math.atan2(hy - 2 * dot * ny,
                                            hx - 2 * dot * nx) % TWO_PI
                        events.append(["thud", pid, round(e["x"], 1), round(e["y"], 1)])
            e["cd"] = max(0.0, e["cd"] - dt)
            e["inv"] = max(0.0, e["inv"] - dt)
            if k["a"] and e["cd"] <= 0:          # hold to auto-fire
                spread = self.rng.uniform(-0.04, 0.04)
                a = e["a"] + spread
                self.bullets.append({
                    "id": self._next_bid,
                    "x": e["x"] + math.cos(e["a"]) * (PLANE_R + 4),
                    "y": e["y"] + math.sin(e["a"]) * (PLANE_R + 4),
                    "vx": math.cos(a) * p["bullet_v"] + math.cos(e["a"]) * spd * 0.4,
                    "vy": math.sin(a) * p["bullet_v"] + math.sin(e["a"]) * spd * 0.4,
                    "life": BULLET_LIFE, "owner": pid,
                })
                self._next_bid += 1
                e["cd"] = p["fire_cd"]
                events.append(["shoot", pid])

        # bullets
        downed = []
        keep = []
        for b in self.bullets:
            b["x"] = (b["x"] + b["vx"] * dt) % WORLD_W
            b["y"] = (b["y"] + b["vy"] * dt) % WORLD_H
            b["life"] -= dt
            if b["life"] <= 0:
                continue
            hit = False
            for ix, iy, ir in self.islands:       # islands eat bullets
                if math.hypot(b["x"] - ix, b["y"] - iy) < ir:
                    events.append(["puff", round(b["x"]), round(b["y"])])
                    hit = True
                    break
            if hit:
                continue
            for pid, e in alive:
                if pid == b["owner"] or not e["alive"] or e["inv"] > 0:
                    continue
                if math.hypot(_wrapd(e["x"] - b["x"], WORLD_W),
                              _wrapd(e["y"] - b["y"], WORLD_H)) < PLANE_R + BULLET_R:
                    hit = True
                    self._damage(pid, e, downed, events)
                    e["a"] += self.rng.uniform(-0.5, 0.5)   # jolt
                    break
            if not hit:
                keep.append(b)
        self.bullets = keep

        # mid-air collisions: bounce apart (with "Ramming hurts" on, both pay a heart)
        for i in range(len(alive)):
            for j in range(i + 1, len(alive)):
                (pa, a), (pb, b) = alive[i], alive[j]
                if not (a["alive"] and b["alive"]):
                    continue
                dx = _wrapd(b["x"] - a["x"], WORLD_W)
                dy = _wrapd(b["y"] - a["y"], WORLD_H)
                dist = math.hypot(dx, dy)
                if dist < PLANE_R * 1.9:
                    nx, ny = (dx / dist, dy / dist) if dist > 0 else (1.0, 0.0)
                    push = PLANE_R * 1.9 - dist + 4
                    a["x"] = (a["x"] - nx * push / 2) % WORLD_W
                    a["y"] = (a["y"] - ny * push / 2) % WORLD_H
                    b["x"] = (b["x"] + nx * push / 2) % WORLD_W
                    b["y"] = (b["y"] + ny * push / 2) % WORLD_H
                    a["a"] += self.rng.uniform(0.6, 1.4)
                    b["a"] -= self.rng.uniform(0.6, 1.4)
                    events.append(["clash",
                                   round(a["x"] + nx * PLANE_R, 1),
                                   round(a["y"] + ny * PLANE_R, 1)])
                    if p["ram"]:
                        if a["inv"] <= 0:
                            self._damage(pa, a, downed, events)
                        if b["inv"] <= 0:
                            self._damage(pb, b, downed, events)

        if downed:
            self.order.append(downed)

        n_alive = sum(1 for e in self.ent.values() if e["alive"])
        if (len(self.ent) > 1 and n_alive <= 1) or (len(self.ent) == 1 and n_alive == 0):
            self._over = True
        elif self.t >= p["cap"]:
            self._over = True

    def _damage(self, pid, e, downed, events):
        e["hp"] -= 1
        e["inv"] = INVULN
        if e["hp"] <= 0:
            e["alive"] = False
            downed.append(pid)
            events.append(["down", pid, round(e["x"], 1), round(e["y"], 1)])
        else:
            events.append(["hitp", pid, round(e["x"], 1), round(e["y"], 1)])

    # ------------------------------------------------------------- snapshots

    def snapshot(self, full=False):
        return {"g": self.ID,
                "e": [[pid, round(e["x"], 1), round(e["y"], 1),
                       1 if e["alive"] else 0,
                       round(min(1.0, 1.0 - e["cd"] / self.p["fire_cd"]), 2),
                       round(e["a"] * 100), e["hp"],
                       1 if e["inv"] > 0 else 0]
                      for pid, e in self.ent.items()],
                "b": [[b["id"], round(b["x"]), round(b["y"]),
                       round(b["vx"]), round(b["vy"])] for b in self.bullets]}

    def setup(self):
        return {"g": self.ID, "w": WORLD_W, "h": WORLD_H,
                "lives": self.p["lives"], "action": "FIRE",
                "islands": [[round(x), round(y), round(r)] for x, y, r in self.islands],
                "gusts": [[round(x), round(y), round(r), round(a * 100)]
                          for x, y, r, a in self.gusts]}

    def status(self):
        alive = sum(1 for e in self.ent.values() if e["alive"])
        return f"{alive} flying · {len(self.bullets)} tracers · {int(self.t)}s / {int(self.p['cap'])}s"

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
        # survivors rank by hearts remaining (ties share)
        survivors = [(pid, e) for pid, e in self.ent.items() if e["alive"]]
        for hp in sorted({e["hp"] for _, e in survivors}):
            groups.append([pid for pid, e in survivors if e["hp"] == hp])
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
        if not me or not me["alive"]:
            return keys
        cfg = {"easy": (0.45, 260, 0.35), "normal": (0.22, 330, 0.7),
               "mean": (0.10, 400, 1.0)}[skill]
        aim_cone, gun_range, lead = cfg
        rng = self.rng

        # nearest living enemy (wrap-aware)
        best, tgt = 1e9, None
        for opid, e in self.ent.items():
            if opid == pid or not e["alive"]:
                continue
            dx = _wrapd(e["x"] - me["x"], WORLD_W)
            dy = _wrapd(e["y"] - me["y"], WORLD_H)
            d = math.hypot(dx, dy)
            if d < best:
                best, tgt = d, (dx, dy, e)
        if not tgt:
            return keys
        dx, dy, e = tgt
        # lead the shot toward where they're going
        tof = best / self.p["bullet_v"]
        dx += math.cos(e["a"]) * self.p["speed"] * tof * lead
        dy += math.sin(e["a"]) * self.p["speed"] * tof * lead
        want = math.atan2(dy, dx)

        # attack-run bias: approach at an angle for a few seconds at a time,
        # straighten out when close — breaks endless mirror tail-chases
        st = self._bot_state.setdefault(pid, {"bias": 0.0, "until": 0.0, "ext": 0.0})
        if self.t >= st["until"]:
            st["bias"] = rng.uniform(-0.55, 0.55)
            st["until"] = self.t + rng.uniform(2.0, 4.0)
        want += st["bias"] * min(1.0, best / 260)

        # when a knife-fight drags on, extend away and come back for a pass
        if best < 110 and self.t >= st["ext"] and rng.random() < 0.035:
            st["ext"] = self.t + rng.uniform(1.2, 2.2)
        extending = self.t < st["ext"]
        if extending:
            want = math.atan2(-dy, -dx)

        # veer around islands looming ahead (they don't shoot back, but still)
        for ix, iy, ir in self.islands:
            ddx, ddy = ix - me["x"], iy - me["y"]
            d = math.hypot(ddx, ddy)
            if d < ir + 85:
                ahead = (math.cos(me["a"]) * ddx + math.sin(me["a"]) * ddy) / max(d, 1)
                if ahead > 0.45:
                    want = math.atan2(me["y"] - iy, me["x"] - ix)
                    break

        diff = (want - me["a"] + math.pi) % TWO_PI - math.pi
        diff += rng.uniform(-0.1, 0.1)
        if self.p["reverse"] < 0:
            diff = -diff       # pre-flip so reversed controls don't lobotomize bots
        keys["l"], keys["r"] = diff < -0.06, diff > 0.06
        keys["u"] = extending or best > 320 or rng.random() < 0.12
        if not extending and best < 70 and abs(diff) < 0.8:   # about to ram: veer off
            keys["d"] = True
            keys["l"], keys["r"] = not keys["l"], keys["l"]
        # fire when lined up; spray a little when nearly lined up
        keys["a"] = not extending and best < gun_range and (
            abs(diff) < aim_cone or (abs(diff) < aim_cone * 2.2 and rng.random() < 0.5))
        return keys


def _wrapd(d, span):
    """Shortest wrapped delta on a torus axis."""
    if d > span / 2:
        return d - span
    if d < -span / 2:
        return d + span
    return d


_NO_KEYS = {"u": False, "d": False, "l": False, "r": False, "a": False}
