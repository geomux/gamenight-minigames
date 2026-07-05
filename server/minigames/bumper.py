"""
bumper.py — BUMPER BALL.

Top-down team soccer brawl. Sumo-style bodies bump around a walled 960x540
pitch; the ball is much lighter than a player, so a good hit sends it flying.
Dash (space) for a harder kick, same feel as Sumo Ring's dash. Knock it fully
past the far wall between the goalposts to score; a short freeze re-spots
everyone after every goal so nobody gets steamrolled leaving their own net.
Nobody is ever eliminated — nametags and all, this is a possession game, not
a survival one.

Teams alternate by roster order (so bots split evenly across both sides):
even indices are RED (spawns on the left, defends the left goal, scores into
the right one), odd indices are BLUE (mirrored). First team to the goal
target wins immediately; at the time cap, whoever's ahead wins (tie = draw).
"""

import math

from .base import MiniGame, BOT_SETTINGS, WORLD_W, WORLD_H

CX, CY = WORLD_W / 2, WORLD_H / 2

# ---- player body (sumo's numbers as the starting point) ----
P_R = 13.0
P_ACCEL = 2500.0
P_DRAG = 4.0
P_VMAX = 600.0
P_BUMP_E = 0.35
P_BUMP_BOOST = 2500.0
WALL_E = 0.5            # player wall-bounce restitution

DASH_V = 430.0
DASH_CD = 2.0
DASH_WINDOW = 0.35      # seconds after a dash during which hits land harder

# ---- ball: light, lively, settles on its own ----
BALL_R = 10.0
BALL_DRAG = 0.8
BALL_WALL_E = 0.8       # restitution off the walls
BALL_MAXV = 700.0
KICK_BASE = 230.0       # flat "solid kick" impulse floor on any contact
KICK_E = 0.5            # extra impulse proportional to closing speed
KICK_DASH_MULT = 1.9    # multiplier while the kicker is inside DASH_WINDOW

# ---- goal mouths: centered openings on the left/right edges ----
GOAL_H = 180.0
GOAL_Y0 = CY - GOAL_H / 2
GOAL_Y1 = CY + GOAL_H / 2

KICKOFF_FREEZE = 1.4    # seconds frozen after a goal while everyone re-spots

# ---- kickoff formation ----
HALF_GAP = 130.0        # x distance from center each team lines up at
ROW_GAP = 60.0          # vertical spacing between teammates
ZIGZAG = 16.0           # small x stagger so a column isn't a dead-straight wall


class BumperBall(MiniGame):
    ID = "bumper"
    NAME = "Bumper Ball"
    TAG = "Sumo physics, soccer rules — bump the ball into their goal."
    CONTROLS = "WASD / arrows to move · SPACE dash"
    MIN_PLAYERS = 2

    @classmethod
    def settings_schema(cls):
        return [
            {"k": "round_time", "label": "Round cap (s)", "type": "choice",
             "def": 120, "choices": [30, 60, 90, 120, 180]},
            {"k": "goals", "label": "Goals to win", "type": "int", "def": 3, "min": 1, "max": 7},
            {"k": "dash", "label": "Dash (space)", "type": "bool", "def": True},
            *BOT_SETTINGS,
        ]

    def __init__(self, roster, settings, rng):
        super().__init__(roster, settings, rng)
        s = settings
        self.p = {
            "cap": float(s.get("round_time", 120)),
            "goals": int(s.get("goals", 3)),
            "dash_v": DASH_V if s.get("dash", True) else 0.0,
        }

        # team 0 = RED (left, defends left/scores right), team 1 = BLUE (mirrored)
        self.team_of = {}
        self.teams = [[], []]
        for i, pl in enumerate(roster):
            team = i % 2
            self.team_of[pl["pid"]] = team
            self.teams[team].append(pl["pid"])
        self._final_team_of = dict(self.team_of)   # immune to mid-round drops

        self.ent = {}
        for pid in self.team_of:
            self.ent[pid] = {
                "x": CX, "y": CY, "vx": 0.0, "vy": 0.0, "r": P_R,
                "keys": _NO_KEYS.copy(), "dash_req": False,
                "cd": 0.0, "dash_t": 99.0,
            }

        # "net": which goal mouth the ball is committed into (None / 0=left /
        # 1=right) — set once its leading edge crosses a goal line inside the
        # mouth, cleared only by the kickoff reset. See _simulate.
        self.ball = {"x": CX, "y": CY, "vx": 0.0, "vy": 0.0, "net": None}
        self.score = [0, 0]
        self.kickoff = 0.0
        self._place_kickoff()          # initial spots; round's own countdown covers the freeze

        self.t = 0.0
        self._bot_state = {}
        self._bot_dt = 1.0 / 30.0
        self._over = False

    # ------------------------------------------------------------- kickoff

    def _spawn_xy(self, team, slot, n):
        x = CX - HALF_GAP if team == 0 else CX + HALF_GAP
        x += ZIGZAG if slot % 2 else -ZIGZAG
        y = CY + (slot - (n - 1) / 2.0) * ROW_GAP
        lo, hi = P_R * 2, WORLD_H - P_R * 2
        return x, max(lo, min(hi, y))

    def _place_kickoff(self):
        self.ball["x"], self.ball["y"] = CX, CY
        self.ball["vx"] = self.ball["vy"] = 0.0
        self.ball["net"] = None
        for team in (0, 1):
            members = self.teams[team]
            n = len(members)
            for slot, pid in enumerate(members):
                e = self.ent.get(pid)
                if not e:
                    continue
                e["x"], e["y"] = self._spawn_xy(team, slot, n)
                e["vx"] = e["vy"] = 0.0
                e["dash_req"] = False

    # ------------------------------------------------------------------ setup

    def setup(self):
        out = {"g": self.ID, "w": WORLD_W, "h": WORLD_H,
               "teams": [list(self.teams[0]), list(self.teams[1])],
               "goalH": GOAL_H}
        if self.p["dash_v"] > 0:
            out["action"] = "DASH"   # drives the big charge meter in the HUD
        return out

    # ------------------------------------------------------------------ input

    def on_input(self, pid, keys):
        e = self.ent.get(pid)
        if e:
            # latch the rising edge here, not in tick() — see sumo.py for why
            # (a tap that presses and releases between two ticks must still count)
            if keys.get("a") and not e["keys"].get("a"):
                e["dash_req"] = True
            e["keys"] = keys

    # ------------------------------------------------------------------- tick

    def tick(self, dt, events):
        if self._over:
            return
        p = self.p
        self.t += dt
        self._bot_dt = dt   # keeps bot_input's per-second rates tick-rate-true

        if self.kickoff > 0:
            # frozen: sim keeps advancing (cooldowns tick, clock runs) but
            # nobody moves and no contact/scoring is resolved
            self.kickoff = max(0.0, self.kickoff - dt)
            for e in self.ent.values():
                e["cd"] = max(0.0, e["cd"] - dt)
                e["dash_t"] += dt
                e["dash_req"] = False
        else:
            self._simulate(dt, events)

        if self.score[0] >= p["goals"] or self.score[1] >= p["goals"]:
            self._over = True
        elif self.t >= p["cap"]:
            self._over = True

    def _simulate(self, dt, events):
        ents = list(self.ent.items())

        # --- players: input, dash, drag, wall bounce ---
        for pid, e in ents:
            k = e["keys"]
            ix = (1.0 if k.get("r") else 0.0) - (1.0 if k.get("l") else 0.0)
            iy = (1.0 if k.get("d") else 0.0) - (1.0 if k.get("u") else 0.0)
            mag = math.hypot(ix, iy)
            if mag > 0:
                ix /= mag; iy /= mag
            e["vx"] += ix * P_ACCEL * dt
            e["vy"] += iy * P_ACCEL * dt
            if e["dash_req"] and e["cd"] <= 0 and self.p["dash_v"] > 0:
                dx, dy = ix, iy
                if dx == 0 and dy == 0:    # no input held: dash along motion
                    sp = math.hypot(e["vx"], e["vy"])
                    if sp > 10:
                        dx, dy = e["vx"] / sp, e["vy"] / sp
                if dx or dy:
                    e["vx"] += dx * self.p["dash_v"]
                    e["vy"] += dy * self.p["dash_v"]
                    e["cd"] = DASH_CD
                    e["dash_t"] = 0.0
                    events.append(["dash", pid])
            e["dash_req"] = False
            e["cd"] = max(0.0, e["cd"] - dt)
            e["dash_t"] += dt
            f = math.exp(-P_DRAG * dt)
            e["vx"] *= f; e["vy"] *= f
            sp = math.hypot(e["vx"], e["vy"])
            lim = P_VMAX * (2.2 if e["dash_t"] < DASH_WINDOW else 1.0)
            if sp > lim:
                e["vx"] *= lim / sp; e["vy"] *= lim / sp
            e["x"] += e["vx"] * dt
            e["y"] += e["vy"] * dt
            # walls are solid for players on all four edges, including across
            # the goal mouths — only the ball is let through (below), so
            # nobody can walk in behind the net and camp the empty goal
            r = e["r"]
            if e["x"] < r:
                e["x"], e["vx"] = r, abs(e["vx"]) * WALL_E
            elif e["x"] > WORLD_W - r:
                e["x"], e["vx"] = WORLD_W - r, -abs(e["vx"]) * WALL_E
            if e["y"] < r:
                e["y"], e["vy"] = r, abs(e["vy"]) * WALL_E
            elif e["y"] > WORLD_H - r:
                e["y"], e["vy"] = WORLD_H - r, -abs(e["vy"]) * WALL_E

        # --- player vs player: sumo-style equal-mass bump ---
        for i in range(len(ents)):
            for j in range(i + 1, len(ents)):
                a, b = ents[i][1], ents[j][1]
                dx = b["x"] - a["x"]; dy = b["y"] - a["y"]
                dist = math.hypot(dx, dy)
                rr = a["r"] + b["r"]
                if dist >= rr or dist == 0:
                    continue
                nx, ny = dx / dist, dy / dist
                push = (rr - dist) / 2
                a["x"] -= nx * push; a["y"] -= ny * push
                b["x"] += nx * push; b["y"] += ny * push
                rvn = (b["vx"] - a["vx"]) * nx + (b["vy"] - a["vy"]) * ny
                if rvn < 0:
                    boost = P_BUMP_BOOST
                    if a["dash_t"] < DASH_WINDOW or b["dash_t"] < DASH_WINDOW:
                        boost *= 1.8
                    imp = -(1 + P_BUMP_E) * rvn / 2 + boost
                    a["vx"] -= nx * imp; a["vy"] -= ny * imp
                    b["vx"] += nx * imp; b["vy"] += ny * imp
                    if imp > 120:
                        events.append(["hit", round((a["x"] + b["x"]) / 2, 1),
                                       round((a["y"] + b["y"]) / 2, 1),
                                       min(1.0, round(imp / 500, 2))])

        # --- ball: drag, wall bounce (open at the goal mouths) ---
        ball = self.ball
        ball["x"] += ball["vx"] * dt
        ball["y"] += ball["vy"] * dt
        f = math.exp(-BALL_DRAG * dt)
        ball["vx"] *= f; ball["vy"] *= f
        sp = math.hypot(ball["vx"], ball["vy"])
        if sp > BALL_MAXV:
            ball["vx"] *= BALL_MAXV / sp; ball["vy"] *= BALL_MAXV / sp
        in_goal_mouth = GOAL_Y0 < ball["y"] < GOAL_Y1
        # once the leading edge crosses a goal line inside the mouth, the
        # ball is COMMITTED to that net: that side's wall stops existing for
        # it entirely — wherever y drifts afterward — until it fully clears
        # the line and scores, or the kickoff reset re-places it. A ball
        # that's visibly in the net must never pop back into play. (Only the
        # committed side opens up; the opposite wall stays solid, and the
        # geometry means players at the mouth can only knock a committed
        # ball deeper in, never back out.)
        if ball["net"] is None and in_goal_mouth:
            if ball["x"] < BALL_R:
                ball["net"] = 0
            elif ball["x"] > WORLD_W - BALL_R:
                ball["net"] = 1
        if not in_goal_mouth:
            if ball["net"] != 0 and ball["x"] < BALL_R:
                ball["x"], ball["vx"] = BALL_R, abs(ball["vx"]) * BALL_WALL_E
            elif ball["net"] != 1 and ball["x"] > WORLD_W - BALL_R:
                ball["x"], ball["vx"] = WORLD_W - BALL_R, -abs(ball["vx"]) * BALL_WALL_E
        if ball["y"] < BALL_R:
            ball["y"], ball["vy"] = BALL_R, abs(ball["vy"]) * BALL_WALL_E
        elif ball["y"] > WORLD_H - BALL_R:
            ball["y"], ball["vy"] = WORLD_H - BALL_R, -abs(ball["vy"]) * BALL_WALL_E

        # --- player vs ball: the ball is "light" — it absorbs the whole
        # impulse and the player barely notices (no reaction applied back) ---
        for pid, e in ents:
            dx = ball["x"] - e["x"]; dy = ball["y"] - e["y"]
            dist = math.hypot(dx, dy)
            rr = e["r"] + BALL_R
            if dist >= rr or dist == 0:
                continue
            nx, ny = dx / dist, dy / dist
            ball["x"] += nx * (rr - dist); ball["y"] += ny * (rr - dist)
            rvn = (ball["vx"] - e["vx"]) * nx + (ball["vy"] - e["vy"]) * ny
            if rvn < 0:
                boost = KICK_BASE
                if e["dash_t"] < DASH_WINDOW:
                    boost *= KICK_DASH_MULT
                imp = -(1 + KICK_E) * rvn + boost
                ball["vx"] += nx * imp; ball["vy"] += ny * imp
                if imp > 120:
                    events.append(["hit", round(ball["x"], 1), round(ball["y"], 1),
                                   min(1.0, round(imp / 500, 2))])

        # --- goals: the ball must be FULLY past the line, not just touching ---
        if ball["x"] + BALL_R < 0:
            self._score(1, events)
        elif ball["x"] - BALL_R > WORLD_W:
            self._score(0, events)

    def _score(self, team, events):
        self.score[team] += 1
        events.append(["goal", team, round(self.ball["x"], 1), round(self.ball["y"], 1)])
        if self.score[0] < self.p["goals"] and self.score[1] < self.p["goals"]:
            self._place_kickoff()
            self.kickoff = KICKOFF_FREEZE

    # ---------------------------------------------------------------- boilerplate

    def snapshot(self, full=False):
        dash_on = self.p["dash_v"] > 0
        out = {"g": self.ID, "score": list(self.score),
               "e": [[pid, round(e["x"], 1), round(e["y"], 1), 1,
                      round(min(1.0, 1.0 - e["cd"] / DASH_CD), 2) if dash_on else 0.0]
                     for pid, e in self.ent.items()],
               "ball": [round(self.ball["x"], 1), round(self.ball["y"], 1),
                        round(self.ball["vx"]), round(self.ball["vy"])]}
        if self.kickoff > 0:
            out["ko"] = round(self.kickoff, 2)
        return out

    def status(self):
        r, b = self.score
        return f"RED {r} - {b} BLUE · {int(self.t)}s / {int(self.p['cap'])}s"

    def drop_player(self, pid):
        self.ent.pop(pid, None)
        team = self.team_of.pop(pid, None)
        if team is not None and pid in self.teams[team]:
            self.teams[team].remove(pid)

    def is_over(self):
        return self._over

    def placements(self):
        r, b = self.score
        if r == b:
            return [(pid, 1) for pid in self._final_team_of]
        winner = 0 if r > b else 1
        return [(pid, 1 if team == winner else 2) for pid, team in self._final_team_of.items()]

    # ------------------------------------------------------------------- bots

    def bot_input(self, pid, skill):
        e = self.ent.get(pid)
        if not e or self.kickoff > 0:
            return _NO_KEYS.copy()
        cfg = _BOT_CFG[skill]
        rng = self.rng
        # tuned per-call at a 30Hz tick; scale by actual dt so lapse/dash
        # frequency and wander speed stay identical at any --tick-rate
        f = self._bot_dt * 30.0

        st = self._bot_state.setdefault(pid, {"wander": rng.uniform(0, 6.28), "lapse": 0.0})
        st["lapse"] = max(0.0, st["lapse"] - self._bot_dt)
        if rng.random() < cfg["lapse"] * f:
            st["lapse"] = rng.uniform(0.3, 0.9)

        team = self.team_of.get(pid, 0)
        own_x = 0.0 if team == 0 else WORLD_W
        enemy_x = WORLD_W if team == 0 else 0.0
        ball = self.ball
        tbx = ball["x"] + ball["vx"] * cfg["lead"]
        tby = ball["y"] + ball["vy"] * cfg["lead"]

        # defender-ish bias: whoever's deepest on their own half hangs back
        # a bit instead of joining every chase (keeps someone home)
        teammates = self.teams[team] or [pid]
        deepest = (min if team == 0 else max)(teammates, key=lambda q: self.ent[q]["x"])
        sweeper = pid == deepest and len(teammates) > 1

        gx, gy = enemy_x - tbx, CY - tby
        gl = math.hypot(gx, gy) or 1.0
        ux, uy = gx / gl, gy / gl
        goal_side = (e["x"] < tbx) if team == 0 else (e["x"] > tbx)
        if goal_side:
            # already between the ball and our own goal: drive through the
            # ball toward the enemy goal instead of just beelining at it
            tx, ty = tbx + ux * 10.0, tby + uy * 10.0
        else:
            # wrong side of the ball (risk of an own-goal push): circle
            # around to get goal-side before engaging
            tx, ty = tbx - ux * 46.0, tby - uy * 46.0

        if sweeper:
            home_x = own_x + (70.0 if team == 0 else -70.0)
            tx = tx * 0.45 + home_x * 0.55
            ty = ty * 0.45 + CY * 0.55

        sx, sy = tx - e["x"], ty - e["y"]
        st["wander"] += rng.uniform(-0.4, 0.4) * math.sqrt(f)
        err = cfg["err"]
        ang = math.atan2(sy, sx) + rng.uniform(-err, err) + math.sin(st["wander"]) * 0.15
        if st["lapse"] > 0:
            ang += rng.uniform(-2.2, 2.2)

        dx, dy = math.cos(ang), math.sin(ang)
        keys = {"u": dy < -0.38, "d": dy > 0.38, "l": dx < -0.38, "r": dx > 0.38, "a": False}

        dist_ball = math.hypot(ball["x"] - e["x"], ball["y"] - e["y"])
        if (self.p["dash_v"] > 0 and e["cd"] <= 0 and dist_ball < 85 + e["r"]
                and rng.random() < cfg["dash"] * f):
            keys["a"] = True
        return keys


_NO_KEYS = {"u": False, "d": False, "l": False, "r": False, "a": False}

_BOT_CFG = {
    "easy":   {"err": 0.55, "lapse": 0.010, "dash": 0.02, "lead": 0.05},
    "normal": {"err": 0.28, "lapse": 0.003, "dash": 0.05, "lead": 0.15},
    "mean":   {"err": 0.12, "lapse": 0.000, "dash": 0.10, "lead": 0.28},
}
