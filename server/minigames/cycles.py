"""
cycles.py — LIGHT CYCLES.

Tron on a grid: everyone moves constantly and leaves a solid trail. Walls,
trails — touch anything, you're out. Last one riding wins. Trails are synced
as *deltas* (only newly claimed cells per snapshot), so bandwidth stays flat
no matter how long the round runs.
"""

import math

from .base import MiniGame, BOT_SETTINGS, WORLD_W, WORLD_H

GW, GH = 96, 54          # grid size (world is 960x540 -> 10px cells)
GRACE = 8.0              # seconds before the walls start closing in
DIRS = {"u": (0, -1), "d": (0, 1), "l": (-1, 0), "r": (1, 0)}
TURN_ORDER = ("u", "r", "d", "l")
_REV = {"u": "d", "d": "u", "l": "r", "r": "l"}

# boost: hold the action key to take an extra movement pass on 2 out of
# every 3 grid-steps (~+67% average speed while sustained), gated by a meter.
BOOST_DRAIN_SECS = 1.75      # full -> empty this fast while boosting flat out
BOOST_REGEN_SECS = 8.5       # empty -> full this slow while not boosting
BOOST_MIN_ENGAGE = 0.12      # meter fraction needed to *start* a boost
BOOST_BOT_CHECK = 0.2        # how often bots reconsider whether to boost (s)
_BOOST_TIGHT_P = {"easy": 0.45, "normal": 0.75, "mean": 1.0}    # escape a pocket
_BOOST_BOT_P = {"easy": 0.08, "normal": 0.22, "mean": 0.45}     # opportunistic use


class LightCycles(MiniGame):
    ID = "cycles"
    NAME = "Light Cycles"
    TAG = "Leave a trail, dodge everyone else's. Last one riding wins."
    CONTROLS = "WASD / arrows to turn · SPACE boost"
    MIN_PLAYERS = 1

    @classmethod
    def settings_schema(cls):
        return [
            {"k": "round_time", "label": "Round cap (s)", "type": "choice",
             "def": 90, "choices": [30, 60, 90, 120, 180]},
            {"k": "speed", "label": "Speed", "type": "choice",
             "def": "normal", "choices": ["slow", "normal", "fast"]},
            {"k": "shrink", "label": "Closing walls", "type": "choice",
             "def": "normal", "choices": ["off", "normal", "fast"]},
            {"k": "wrap", "label": "Wraparound edges", "type": "bool", "def": False},
            {"k": "trails", "label": "Dead trails", "type": "choice",
             "def": "stay", "choices": ["stay", "vanish"]},
            {"k": "reverse", "label": "Reverse controls", "type": "bool", "def": False},
            {"k": "boost", "label": "Boost", "type": "bool", "def": True},
            *BOT_SETTINGS,
        ]

    def __init__(self, roster, settings, rng):
        super().__init__(roster, settings, rng)
        s = settings
        self.cps = {"slow": 7.0, "normal": 9.5, "fast": 12.5}[s.get("speed", "normal")]
        self.cap = float(s.get("round_time", 90))
        self.wrap = bool(s.get("wrap"))
        self.vanish = s.get("trails") == "vanish"
        self.reverse = bool(s.get("reverse"))
        self.boost_enabled = bool(s.get("boost", True))
        shrink = "off" if self.wrap else s.get("shrink", "normal")
        self.shrink_every = {"off": 0.0, "normal": 6.0, "fast": 3.5}[shrink]

        self.grid = bytearray(GW * GH)      # 0 empty, else pid (pids start at 1)
        self.heads = {}
        self.margin = 0
        self.t = 0.0
        self.acc = 0.0
        self.order = []                      # death order: groups of pids
        self.new_cells = []                  # cells claimed since last snapshot
        self._over = False
        self._next_shrink = GRACE

        # spawn on an inner ellipse, all riding clockwise (tangential = fair)
        n = max(1, len(roster))
        for i, pl in enumerate(roster):
            ang = 2 * math.pi * i / n
            x = int(GW / 2 + math.cos(ang) * GW * 0.33)
            y = int(GH / 2 + math.sin(ang) * GH * 0.33)
            x = max(2, min(GW - 3, x))
            y = max(2, min(GH - 3, y))
            tang = ang + math.pi / 2
            dx, dy = math.cos(tang), math.sin(tang)
            d = ("r" if dx > 0 else "l") if abs(dx) > abs(dy) else ("d" if dy > 0 else "u")
            while self.grid[y * GW + x]:     # nudge off an occupied spawn
                x = (x + 1) % GW
            self.heads[pl["pid"]] = {"x": x, "y": y, "dir": d, "alive": True,
                                     "keys": {},
                                     "boost": 1.0 if self.boost_enabled else 0.0,
                                     "boosting": False, "boost_parity": 0}
            self._claim(x, y, pl["pid"])

    # ------------------------------------------------------------------ grid

    def _claim(self, x, y, pid):
        self.grid[y * GW + x] = pid
        self.new_cells.append([x, y, pid])

    def _blocked(self, x, y):
        if x < self.margin or x >= GW - self.margin or y < self.margin or y >= GH - self.margin:
            return True
        return self.grid[y * GW + x] != 0

    # ----------------------------------------------------------------- input

    def on_input(self, pid, keys):
        h = self.heads.get(pid)
        if h and h["alive"]:
            h["keys"] = keys

    # ------------------------------------------------------------------ tick

    def tick(self, dt, events):
        if self._over:
            return
        self.t += dt

        # closing walls
        if self.shrink_every and self.t >= self._next_shrink and self.margin < min(GW, GH) // 2 - 4:
            self.margin += 1
            self._next_shrink += self.shrink_every
            events.append(["wall", self.margin])
            crushed = []
            for pid, h in self.heads.items():
                if h["alive"] and self._in_wall(h["x"], h["y"]):
                    crushed.append(pid)
            self._kill(crushed, events)

        # boost meter: drains while actively boosting, regens otherwise. A
        # small min-charge gate to *start* a boost (but not to keep one going)
        # avoids stutter as the meter crosses that threshold.
        if self.boost_enabled:
            for pid, h in self.heads.items():
                if not h["alive"]:
                    continue
                want = h["keys"].get("a", False)
                if h["boosting"]:
                    if not want or h["boost"] <= 0.0:
                        h["boosting"] = False
                elif want and h["boost"] > BOOST_MIN_ENGAGE:
                    h["boosting"] = True
                    events.append(["boost", pid])
                if h["boosting"]:
                    h["boost"] = max(0.0, h["boost"] - dt / BOOST_DRAIN_SECS)
                else:
                    h["boost"] = min(1.0, h["boost"] + dt / BOOST_REGEN_SECS)

        # fixed cell-step cadence
        self.acc += dt
        step = 1.0 / self.cps
        while self.acc >= step and not self._over:
            self.acc -= step
            self._step(events)

        if not self._over and self.t >= self.cap:
            self._over = True

    def _in_wall(self, x, y):
        return (x < self.margin or x >= GW - self.margin
                or y < self.margin or y >= GH - self.margin)

    def _step(self, events):
        alive = [(pid, h) for pid, h in self.heads.items() if h["alive"]]

        # turns: a held perpendicular key turns you at the next cell
        for pid, h in alive:
            cur = DIRS[h["dir"]]
            for key in TURN_ORDER:
                held = h["keys"].get(key)
                if not held:
                    continue
                k = key
                if self.reverse:
                    k = _REV[k]
                nd = DIRS[k]
                if nd[0] == -cur[0] and nd[1] == -cur[1]:   # no 180s
                    continue
                if nd == cur:
                    continue
                h["dir"] = k
                break

        self._advance(alive, events)
        if self._over:
            return

        # boost: a bonus movement pass on 2 out of every 3 boosted steps
        # (~+67% average speed while sustained). The first boosted step of a
        # fresh engagement always gets the bonus, for a snappy tap response.
        boosters = []
        for pid, h in alive:
            if not h["alive"]:
                continue
            if not h["boosting"]:
                h["boost_parity"] = 0
                continue
            if h["boost_parity"] != 2:
                boosters.append((pid, h))
            h["boost_parity"] = (h["boost_parity"] + 1) % 3
        if boosters:
            self._advance(boosters, events)

    def _advance(self, movers, events):
        """One grid-cell movement pass for `movers` ([(pid, head), …]):
        propose, resolve collisions against the grid and each other, move the
        survivors, kill the rest. Used for the normal per-step move and for a
        boosting head's bonus sub-step, so both tunnel-check identically."""
        prop = {}
        for pid, h in movers:
            dx, dy = DIRS[h["dir"]]
            tx, ty = h["x"] + dx, h["y"] + dy
            if self.wrap:
                tx %= GW
                ty %= GH
            prop[pid] = (tx, ty)

        dead = set()
        # walls / trails / out of bounds
        for pid, h in movers:
            tx, ty = prop[pid]
            if not self.wrap and (tx < 0 or tx >= GW or ty < 0 or ty >= GH):
                dead.add(pid)
            elif self._blocked(tx % GW, ty % GH):
                dead.add(pid)
        # head-on swaps
        pos = {pid: (h["x"], h["y"]) for pid, h in movers}
        for i in range(len(movers)):
            for j in range(i + 1, len(movers)):
                a, b = movers[i][0], movers[j][0]
                if prop[a] == pos[b] and prop[b] == pos[a]:
                    dead.add(a)
                    dead.add(b)
        # same target cell
        targets = {}
        for pid, _ in movers:
            targets.setdefault(prop[pid], []).append(pid)
        for cell, pids in targets.items():
            if len(pids) > 1:
                dead.update(pids)

        # movers move
        for pid, h in movers:
            if pid in dead:
                continue
            h["x"], h["y"] = prop[pid]
            self._claim(h["x"], h["y"], pid)

        self._kill(sorted(dead), events)

    def _kill(self, pids, events):
        group = []
        for pid in pids:
            h = self.heads.get(pid)
            if not h or not h["alive"]:
                continue
            h["alive"] = False
            group.append(pid)
            events.append(["die", pid, h["x"], h["y"]])
            if self.vanish:
                for i in range(GW * GH):
                    if self.grid[i] == pid:
                        self.grid[i] = 0
                events.append(["clear", pid])
        if group:
            self.order.append(group)

        n_alive = sum(1 for h in self.heads.values() if h["alive"])
        if len(self.heads) > 1 and n_alive <= 1:
            self._over = True
        elif len(self.heads) == 1 and n_alive == 0:
            self._over = True

    # ------------------------------------------------------------- snapshots

    def snapshot(self, full=False):
        heads = [[pid, h["x"], h["y"], 1 if h["alive"] else 0,
                  round(h["boost"], 2),
                  DIRS[h["dir"]][0], DIRS[h["dir"]][1],
                  1 if h["boosting"] else 0]
                 for pid, h in self.heads.items()]
        if full:
            cells = [[i % GW, i // GW, self.grid[i]]
                     for i in range(GW * GH) if self.grid[i]]
        else:
            cells, self.new_cells = self.new_cells, []
        return {"g": self.ID, "heads": heads, "cells": cells, "margin": self.margin}

    def setup(self):
        out = {"g": self.ID, "w": WORLD_W, "h": WORLD_H,
               "gw": GW, "gh": GH, "wrap": self.wrap}
        if self.boost_enabled:
            out["action"] = "BOOST"
        return out

    def status(self):
        alive = sum(1 for h in self.heads.values() if h["alive"])
        return f"{alive} riding · walls +{self.margin} · {int(self.t)}s / {int(self.cap)}s"

    def drop_player(self, pid):
        h = self.heads.get(pid)
        if h and h["alive"]:
            h["alive"] = False
            self.order.insert(0, [pid])
            n_alive = sum(1 for q in self.heads.values() if q["alive"])
            if len(self.heads) > 1 and n_alive <= 1:
                self._over = True

    def is_over(self):
        return self._over

    def placements(self):
        groups = list(self.order)
        survivors = [pid for pid, h in self.heads.items() if h["alive"]]
        if survivors:
            groups.append(survivors)
        res = []
        placed_better = 0
        for grp in reversed(groups):
            for pid in grp:
                res.append((pid, placed_better + 1))
            placed_better += len(grp)
        res.sort(key=lambda t: t[1])
        return res

    # ------------------------------------------------------------------ bots

    def bot_input(self, pid, skill):
        h = self.heads.get(pid)
        keys = {"u": False, "d": False, "l": False, "r": False, "a": False}
        if not h or not h["alive"]:
            return keys
        cfg = {"easy": (5.0, 2.5, 8), "normal": (2.0, 1.2, 12), "mean": (0.6, 0.6, 16)}[skill]
        jitter, straight_bonus, look = cfg

        cur = h["dir"]
        cx, cy = DIRS[cur]
        options = {cur: (cx, cy),
                   _left(cur): DIRS[_left(cur)],
                   _right(cur): DIRS[_right(cur)]}
        best_key, best_score, best_free = cur, -1e9, look
        for key, (dx, dy) in options.items():
            free = 0
            x, y = h["x"], h["y"]
            for _ in range(look):
                x, y = x + dx, y + dy
                if self.wrap:
                    x %= GW
                    y %= GH
                elif x < 0 or x >= GW or y < 0 or y >= GH:
                    break
                if self._blocked(x, y):
                    break
                free += 1
            score = free + self.rng.uniform(-jitter, jitter)
            if key == cur:
                score += straight_bonus
            if score > best_score:
                best_key, best_score, best_free = key, score, free
        if best_key != cur:
            # emit the *absolute* key; reverse-controls flips it server-side,
            # so pre-flip for bots to keep them competent
            k = best_key
            if self.reverse:
                k = _REV[k]
            keys[k] = True

        if self.boost_enabled and h["boost"] > BOOST_MIN_ENGAGE:
            keys["a"] = self._bot_wants_boost(pid, h, skill, best_free, look)
        return keys

    def _bot_wants_boost(self, pid, h, skill, best_free, look):
        """Meaningful boost use: escape a closing pocket, or opportunistically
        race/cut off a nearby rival. Decisions are throttled to a fixed
        real-time cadence (BOOST_BOT_CHECK) so frequency doesn't depend on
        tick rate; once triggered, held for a short window so it isn't a
        single-tick flicker."""
        if self.t < h.get("bot_boost_until", 0.0):
            return True
        if self.t < h.get("bot_next_check", 0.0):
            return False
        h["bot_next_check"] = self.t + BOOST_BOT_CHECK
        tight = best_free < look * 0.5
        go = tight and self.rng.random() < _BOOST_TIGHT_P[skill]
        if not go:
            for opid, other in self.heads.items():
                if (opid != pid and other["alive"]
                        and abs(other["x"] - h["x"]) + abs(other["y"] - h["y"]) <= look):
                    go = self.rng.random() < _BOOST_BOT_P[skill] * 2
                    break
        if not go:
            go = self.rng.random() < _BOOST_BOT_P[skill]
        if go:
            h["bot_boost_until"] = self.t + (self.rng.uniform(0.5, 1.0) if tight
                                              else self.rng.uniform(0.3, 0.6))
        return go


def _left(d):
    return {"u": "l", "l": "d", "d": "r", "r": "u"}[d]


def _right(d):
    return {"u": "r", "r": "d", "d": "l", "l": "u"}[d]
